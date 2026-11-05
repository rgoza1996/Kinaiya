"""One exact source grant and retained original calls for a task orchestrator.

This module neither reads credentials nor opens network connections. The
trusted provider adapter must call its supplied current() immediately before
dispatch and own transport cleanup. Generated code receives only bounded JSON.
"""

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from packages.contracts.agent_media import (
    ConsultMediaRequest,
    MediaConsultationResult,
    MediaProviderResult,
    MediaSourceGrant,
    MediaTarget,
    canonical,
    digest,
)


class MediaToolError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__("Media consultation refused: " + code)


@dataclass(frozen=True, slots=True)
class PreparedMediaConsultation:
    request: ConsultMediaRequest
    grant: MediaSourceGrant
    target: MediaTarget
    source_bytes: bytes
    request_sha256: str


def _identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _snapshot(policy, grant):
    """Pin directories/file, then copy and hash the entire stable original."""
    try:
        resolved = policy.check_read(grant.source_path)
        if resolved != policy.workspace / Path(grant.source_path):
            raise MediaToolError("source_unavailable")
        with policy.scoped_parent(grant.source_path) as (parent, name, _):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or before.st_size != grant.source_bytes:
                    raise MediaToolError("source_changed")
                chunks, size = [], 0
                while True:
                    chunk = os.read(descriptor, min(65536, grant.source_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > grant.source_bytes:
                        raise MediaToolError("source_changed")
                after = os.fstat(descriptor)
                policy.check_workspace()
                if _identity(before) != _identity(after) or size != grant.source_bytes:
                    raise MediaToolError("source_changed")
            finally:
                os.close(descriptor)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != grant.source_sha256:
            raise MediaToolError("source_changed")
        # This is only an MP4 signature check; decoding and frame/timing
        # qualification remain the native provider adapter's responsibility.
        if data[4:8] != b"ftyp":
            raise MediaToolError("source_unavailable")
        return data
    except MediaToolError:
        raise
    except Exception:
        raise MediaToolError("source_unavailable") from None


class WorkspaceMediaBroker:
    def __init__(self, *, policy, grant, provider, current):
        self.policy = policy
        self.grant = MediaSourceGrant.model_validate(grant)
        self.provider = provider
        self.current = current
        self._calls = {}
        self._completed = {}
        self._closed = False
        self._cancelled = set()

    @property
    def required_operations(self):
        return frozenset(self.grant.required_operations)

    def _check_authority(self):
        try:
            self.policy.check_workspace()
            self.current()
        except Exception:
            raise MediaToolError("stale") from None

    def _check(self):
        if self._closed:
            raise MediaToolError("stale")
        self._check_authority()

    def check_primary_export(self, provider_info, result):
        """Authorize only a retained exact original result at its pinned target.

        Closing call custody does not revoke a still-current native disclosure
        grant. The host must retain this check at subsequent count/generation
        exports, and separately refuse unrelated private history and tool data.
        """
        self._check_authority()
        target = self.grant.primary_result_target
        if target is None:
            raise MediaToolError("disclosure_refused")
        try:
            info = (
                provider_info
                if type(provider_info) is dict
                else provider_info.model_dump(mode="json")
            )
            if (
                type(info) is not dict
                or type(info.get("provider")) is not str
                or type(info.get("model")) is not str
                or info.get("local") is not False
                or any(
                    info.get(field) != getattr(target, field)
                    for field in ("provider", "model", "local")
                )
            ):
                raise ValueError()
            observed = MediaConsultationResult.model_validate(result)
            if (
                observed.binding != self.grant.binding
                or observed.source_path != self.grant.source_path
                or observed.source_sha256 != self.grant.source_sha256
                or observed.source_bytes != self.grant.source_bytes
                or observed.content_scope != self.grant.content_scope
                or self._completed.get(observed.operation)
                != canonical(observed.model_dump(mode="json"))
            ):
                raise ValueError()
        except Exception:
            raise MediaToolError("disclosure_refused") from None

    async def call(self, request):
        try:
            request = ConsultMediaRequest.model_validate(request)
        except Exception:
            raise MediaToolError("invalid_request") from None
        self._check()
        grant = self.grant
        if (request.source_path, request.source_sha256) != (grant.source_path, grant.source_sha256):
            raise MediaToolError("disclosure_refused")
        target = next((item for item in grant.targets if item.operation == request.operation), None)
        if target is None:
            raise MediaToolError("operation_unavailable")
        logical = digest(
            {"grant": grant.model_dump(mode="json"), "request": request.model_dump(mode="json")}
        )
        old = self._calls.get(request.operation)
        if old is not None:
            if old[0] != logical:
                raise MediaToolError("operation_conflict")
            result = await asyncio.shield(old[1])
            self._check()
            return MediaConsultationResult.model_validate(result)
        # No await separates the source snapshot, authority check and enrollment.
        # A cancelled observer cannot dispatch a replacement original.
        source = _snapshot(self.policy, grant)
        self._check()
        prepared = PreparedMediaConsultation(request, grant, target, source, logical)
        task = asyncio.create_task(self._run(prepared))
        self._calls[request.operation] = (logical, task)
        result = await asyncio.shield(task)
        self._check()
        return MediaConsultationResult.model_validate(result)

    async def _run(self, prepared):
        try:
            self._check()
            observed = await self.provider(prepared, self._check)
            self._check()
            observed = MediaProviderResult.model_validate(observed)
            if observed.model != prepared.target.model:
                raise MediaToolError("result_invalid")
            body = {
                **observed.model_dump(mode="json"),
                "status": "completed",
                "operation": prepared.request.operation,
                "binding": prepared.grant.binding.model_dump(mode="json"),
                "source_path": prepared.grant.source_path,
                "source_sha256": prepared.grant.source_sha256,
                "source_bytes": prepared.grant.source_bytes,
                "content_scope": prepared.grant.content_scope,
                "request_sha256": prepared.request_sha256,
            }
            result = MediaConsultationResult.model_validate({**body, "result_sha256": digest(body)})
            self._completed[prepared.request.operation] = canonical(result.model_dump(mode="json"))
            return result
        except (asyncio.CancelledError, MediaToolError):
            raise
        except Exception:
            raise MediaToolError("provider_failed") from None

    async def close(self, *, timeout=5.0):
        """Join provider coroutines; pending originals remain retained on False.

        This is not independent physical transport proof. The trusted adapter
        must settle its original body/client before returning or raising.
        """
        self._closed = True
        originals = tuple(task for _, task in self._calls.values())
        for task in originals:
            if not task.done() and task not in self._cancelled:
                self._cancelled.add(task)
                task.cancel()
        if not originals:
            return True
        done, pending = await asyncio.wait(originals, timeout=timeout)
        for task in done:
            # Retrieve failures; a completed failure never authorizes replay.
            if not task.cancelled():
                task.exception()
        return not pending
