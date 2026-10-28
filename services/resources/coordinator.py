"""Explicit measured conversation-worker handoff; core services stay independent."""

import asyncio
import hashlib
import json
import os
import sqlite3
import stat

from packages.contracts.models import AppendRequest, new_id
from packages.resources.processes import _mem_available_bytes
from services.converse.intent import ConverseIntent
from services.resources.state import PROFILES


class WhistleAdmissionError(RuntimeError):
    def __init__(self, *, cleanup_confirmed=False):
        super().__init__("Whistle resource admission is unavailable")
        self.cleanup_confirmed = cleanup_confirmed


class ResourceCoordinator:
    def __init__(
        self,
        manager,
        state,
        converse,
        *,
        journal=None,
        task_status=None,
        memory_probe=_mem_available_bytes,
        minimum_free_bytes=128 * 1024 * 1024,
        local_converse_unmanaged=False,
        before_start=None,
        restore_reserve_bytes=0,
        whistle_restore_reserve_bytes=0,
        gpu_probe=None,
        gpu_restore_reserve_bytes=0,
    ):
        self.manager, self.state, self.converse = manager, state, converse
        self.intent = ConverseIntent(
            state.path.parent / "converse-intent.json",
            initial_enabled=state.record.desired_converse,
        )
        self.controller_id = new_id()
        self.journal, self.task_status = journal, task_status
        self.memory_probe, self.minimum = memory_probe, minimum_free_bytes
        self.local_converse_unmanaged = local_converse_unmanaged
        self.before_start = before_start
        self.restore_reserve = restore_reserve_bytes
        self.whistle_restore_reserve = whistle_restore_reserve_bytes
        self.gpu_probe = gpu_probe
        self.gpu_restore_reserve = gpu_restore_reserve_bytes
        self.gpu_release = None
        self.lock = asyncio.Lock()
        self.publication_pending = False
        self._whistle_preemptions = 0
        self._has_whistle = "whistle" in self.manager.status()["workers"]
        database = state.path.parent / "resource-actions.sqlite"
        fd = os.open(
            database, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError("Resource action ledger must be an owner-only regular file")
        finally:
            os.close(fd)
        self.db = sqlite3.connect(database)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS actions (key TEXT PRIMARY KEY, digest TEXT, result TEXT)"
        )
        self.db.commit()

    def status(self):
        gpu = self.gpu_probe() if self.gpu_probe is not None else None
        choice = self.intent.read()
        policy = {
            **self.state.record.model_dump(),
            "desired_converse": choice.enabled,
            "owner_intent_revision": choice.revision,
        }
        return {
            "schema_version": "1.0",
            "controller_id": self.controller_id,
            "policy": policy,
            **self.manager.status(),
            "memory_available_bytes": self.memory_probe(),
            "coverage": (
                "managed local voice/Laya processes; AMD driver counters; no hard reservation"
                if self.gpu_probe is not None
                else "managed local voice processes; no GPU allocator proof"
            ),
            "gpu_memory": gpu,
            "gpu_release_observation": self.gpu_release,
            "local_text_worker_unmanaged": self.local_converse_unmanaged,
            "client_notification_pending": self.publication_pending,
            "physical_playback_stopped": "unconfirmed",
        }

    async def _publish(self):
        if self.journal is None:
            return
        record = self.state.record
        for session in (await self.journal.sessions())[:128]:
            if session.channel != "converse":
                continue
            await self.journal.append(
                AppendRequest(
                    session_id=session.session_id,
                    thread_id=session.thread_id,
                    epoch=session.epoch,
                    kind="resource.changed",
                    payload={
                        "phase": record.phase,
                        "revision": record.revision,
                        "converse_enabled": record.desired_converse and record.phase == "ready",
                    },
                    idempotency_key=f"resource:{record.revision}:{session.session_id}",
                )
            )

    async def _save(self, **changes):
        choice = self.intent.read()
        changes["desired_converse"] = choice.enabled
        if (changes.get("phase") == "ready" and choice.enabled) or (
            changes.get("phase") == "off" and not choice.enabled
        ):
            changes["owner_intent_revision"] = choice.revision
        self.state.save(**changes)
        try:
            await asyncio.wait_for(self._publish(), timeout=1)
            self.publication_pending = False
        except Exception:
            self.publication_pending = True

    async def initialize(self):
        async with self.lock:
            if self.state.record.lease_id or self.state.record.phase in {
                "quiescing",
                "unloading",
                "local_work",
                "restoring",
            }:
                await self._save(
                    phase="failed",
                    reason="Coordinator restarted with uncertain resource ownership; inspect before restoring",
                )
                return
            try:
                health = await self.converse.health()
                self._provider_locality(health)
                await self._save(phase="initializing")
                if self.intent.read().enabled:
                    await self._restore_locked()
                else:
                    await self._off_locked()
            except Exception:
                await self._save(
                    phase="failed", reason="Resource startup failed; inspect worker status"
                )

    def _provider_locality(self, health):
        local = health.get("provider", {}).get("local")
        if type(local) is not bool:
            raise RuntimeError("Converse provider locality is unavailable; handoff refused")
        self.local_converse_unmanaged = local

    async def action(self, payload):
        # A new authenticated off/handoff request must beat a Whistle restart
        # already awaiting readiness under the lifecycle lock. A replay/conflict
        # must not revoke legitimate current work. This is only a Whistle gate;
        # the existing durable action implementation remains the authority.
        preempt = self._has_whistle and (
            payload.get("operation") == "handoff"
            or (payload.get("operation") == "converse" and payload.get("enabled") is False)
        )
        if preempt:
            preempt = (
                self.db.execute(
                    "SELECT 1 FROM actions WHERE key=?", (payload["idempotency_key"],)
                ).fetchone()
                is None
            )
        if preempt and payload["operation"] == "handoff":
            preempt = await self._handoff_may_preempt(payload)
            if preempt:
                # Another same-key action can finish while preview awaits,
                # including a rejection that changes no resource revision.
                # Replay/conflict remains exclusively _action's authority.
                preempt = (
                    self.db.execute(
                        "SELECT 1 FROM actions WHERE key=?", (payload["idempotency_key"],)
                    ).fetchone()
                    is None
                )
        if preempt:
            self._whistle_preemptions += 1
        try:
            return await self._action(payload)
        finally:
            if preempt:
                self._whistle_preemptions -= 1

    async def _handoff_may_preempt(self, payload):
        """Read-only eligibility preview, never resource admission authority.

        An already-ineligible handoff must not revoke recovery while waiting
        for the lifecycle lock. Keep all authoritative under-lock checks in
        _handoff_locked, including actual post-release memory measurements.
        """
        revision = self.state.record.revision
        if self.state.record.profile == "conversation_priority" or self.state.record.lease_id:
            return False
        required, required_gpu = (
            payload.get("required_bytes", 0),
            payload.get("required_gpu_bytes", 0),
        )
        if (
            type(required) is not int
            or not self.minimum <= required <= 1 << 46
            or type(required_gpu) is not int
            or not 0 <= required_gpu <= 1 << 46
            or (required_gpu and self.gpu_probe is None)
        ):
            return False
        try:
            # Do not mutate the cached locality observation outside the lock.
            health = await self.converse.health()
            if health.get("provider", {}).get("local") is not False:
                return False
            if self.gpu_probe is not None:
                gpu = self.gpu_probe()
                if (
                    gpu is None
                    or gpu["worker_resident_bytes"] <= 0
                    or required_gpu > gpu["total_bytes"]
                ):
                    return False
            task_id = payload.get("task_id")
            if task_id and self.task_status:
                task = await self.task_status(task_id)
                if task.get("state") in {"succeeded", "failed", "cancelled", "paused"}:
                    return False
        except Exception:
            # Uncertainty is not permission to disrupt. The existing action
            # path records its authoritative result after acquiring the lock.
            return False
        return self.state.record.revision == revision

    def whistle_policy(self):
        record, choice = self.state.record, self.intent.read()
        if (
            self._whistle_preemptions
            or record.phase != "ready"
            or record.lease_id
            or not record.desired_converse
            or not choice.enabled
            or record.owner_intent_revision != choice.revision
        ):
            raise WhistleAdmissionError(cleanup_confirmed=True)
        return record.revision, choice.revision

    @staticmethod
    def _whistle_stopped(result):
        evidence = result.get("stop_evidence", {})
        return bool(
            result.get("ok")
            and (
                result.get("superseded")
                or result.get("release_confirmed")
                or (evidence.get("reaped") and evidence.get("group_empty"))
            )
        )

    async def admit_whistle(self, expected_policy):
        """Fixed-worker new-request recovery, never uncertain-inference replay.

        The broker retains this operation independently of HTTP cancellation.
        Off/handoff barriers and durable owner revisions are rechecked after
        startup; a newly ready but revoked incarnation is stopped before return.
        """
        async with self.lock:
            if self.whistle_policy() != expected_policy:
                raise WhistleAdmissionError(cleanup_confirmed=True)
            identity = self.manager.owned_identity("whistle")
            if identity is None:
                if self.before_start:
                    try:
                        await self.before_start("whistle")
                    except Exception as exc:
                        # No start has been issued for this request. Existing
                        # manager ownership, if any, remains separately retained.
                        raise WhistleAdmissionError(cleanup_confirmed=True) from exc
                if self.whistle_policy() != expected_policy:
                    raise WhistleAdmissionError(cleanup_confirmed=True)
                # Only the stopped fixed Whistle allocation is being restored.
                # Voice/Laya may already be resident; do not double-credit their
                # aggregate restore reserve or treat missing measurement as free.
                available = self.memory_probe()
                if available is None or available < max(self.minimum, self.whistle_restore_reserve):
                    raise WhistleAdmissionError(cleanup_confirmed=True)
                try:
                    result = await self.manager.start("whistle")
                except Exception as exc:
                    stopped = await self.manager.stop("whistle")
                    raise WhistleAdmissionError(
                        cleanup_confirmed=self._whistle_stopped(stopped)
                    ) from exc
                identity = self.manager.owned_identity("whistle")
                if not result.get("ok") or not result.get("ready") or identity is None:
                    stopped = await self.manager.stop("whistle")
                    raise WhistleAdmissionError(cleanup_confirmed=self._whistle_stopped(stopped))
            try:
                if self.whistle_policy() != expected_policy:
                    raise WhistleAdmissionError()
            except (OSError, ValueError, WhistleAdmissionError) as exc:
                stopped = await self.manager.stop_if_current(identity)
                raise WhistleAdmissionError(
                    cleanup_confirmed=self._whistle_stopped(stopped)
                ) from exc
            return identity

    async def _action(self, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        key, operation = payload["idempotency_key"], payload["operation"]
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self.lock:
            row = self.db.execute(
                "SELECT digest,result FROM actions WHERE key=?", (key,)
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("Resource idempotency key conflicts with earlier input")
                return (
                    json.loads(row[1])
                    if row[1]
                    else {
                        "ok": False,
                        "uncertain": True,
                        "error": "Interrupted resource action; inspect status before a new request",
                    }
                )
            with self.db:
                self.db.execute("INSERT INTO actions VALUES(?,?,NULL)", (key, digest))
            try:
                if operation == "profile":
                    profile = payload.get("profile")
                    if profile not in PROFILES:
                        raise ValueError("Unknown resource profile")
                    if self.state.record.lease_id:
                        raise ValueError(
                            "Finish or restore the current resource lease before changing profiles"
                        )
                    await self._save(profile=profile)
                elif operation == "handoff":
                    await self._handoff_locked(payload)
                elif operation == "restore":
                    await self._restore_locked()
                elif operation == "converse":
                    desired = payload["enabled"]
                    choice = self.intent.set(desired)
                    await self._save(owner_intent_revision=choice.revision)
                    if desired:
                        await self._restore_locked()
                    else:
                        await self._off_locked()
                elif operation == "task_finished":
                    if (
                        self.state.record.phase == "local_work"
                        and self.state.record.lease_task_id == payload.get("task_id")
                    ):
                        await self._restore_locked()
                else:
                    raise ValueError("Unknown resource action")
                result = {"ok": True, "policy": self.state.record.model_dump()}
            except asyncio.CancelledError:
                self.state.save(
                    phase="failed", reason="Resource action interrupted; inspect status"
                )
                raise
            except ValueError as exc:
                result = {
                    "ok": False,
                    "error": str(exc)[:160],
                    "policy": self.state.record.model_dump(),
                }
            except Exception as exc:
                await self._save(phase="failed", reason="Resource action failed; inspect status")
                result = {
                    "ok": False,
                    "error": str(exc)[:160],
                    "policy": self.state.record.model_dump(),
                }
            with self.db:
                self.db.execute(
                    "UPDATE actions SET result=? WHERE key=?", (json.dumps(result), key)
                )
            return result

    async def reconcile_owner_intent(self):
        """Observe legacy owner controls without holding their acknowledgment up.

        A file revision is the wakeup, not permission for automatic retries of a
        failed restoration. Each explicit owner request advances that revision.
        """
        async with self.lock:
            choice = self.intent.read()
            if choice.revision == self.state.record.owner_intent_revision:
                if not choice.enabled or self.state.record.phase != "ready":
                    return
                # A freshly restarted managed Converse begins suspended. Reuse
                # verified workers before reopening its resource gate; its owner
                # choice remains independent and is checked again by restoration.
                try:
                    health = await self.converse.health()
                    self._provider_locality(health)
                except Exception:
                    return
                if health.get("resource_enabled") is not False:
                    return
            # Handle this explicit intent once. A newer revision arriving while
            # a worker starts remains pending, even if a policy snapshot sees it.
            await self._save(owner_intent_revision=choice.revision)
            try:
                if choice.enabled:
                    await self._restore_locked()
                else:
                    await self._off_locked()
            except Exception:
                await self._save(phase="failed", reason="Owner control reconciliation failed")

    async def _off_locked(self):
        # The durable owner gate already blocks inference even if Converse's
        # HTTP service is unavailable. Stop only workers this controller owns.
        control_error = None
        try:
            await self.converse.enabled(False)
        except Exception as exc:
            control_error = exc
        await self._stop_workers()
        await self._save(
            phase="off",
            lease_id=None,
            lease_task_id=None,
            required_bytes=0,
            required_gpu_bytes=0,
        )
        if control_error is not None:
            raise RuntimeError(
                "Workers stopped; Converse control acknowledgment is unavailable"
            ) from control_error

    async def _stop_workers(self):
        results = []
        for worker_id in self.manager.status()["workers"]:
            result = await self.manager.stop(worker_id)
            if not result.get("ok"):
                raise RuntimeError("Managed worker termination is unconfirmed")
            evidence = result.get("stop_evidence")
            if evidence is not None and (
                not evidence.get("reaped") or not evidence.get("group_empty")
            ):
                raise RuntimeError("Managed worker group release is unconfirmed")
            if worker_id == "whistle" and result.get("already"):
                row = self.manager.status()["workers"][worker_id]
                retained = row.get("stop_evidence") or {}
                if (
                    row.get("state") == "stopped"
                    and row.get("owned_pgid") is None
                    and retained.get("reaped")
                    and retained.get("group_empty")
                ):
                    result = {**result, "release_confirmed": True}
            results.append(result)
        return results

    async def _handoff_locked(self, payload):
        if self.state.record.profile == "conversation_priority":
            raise ValueError(
                "Conversation priority queues demanding local work; select local work priority for handoff"
            )
        self._provider_locality(await self.converse.health())
        if self.local_converse_unmanaged:
            raise ValueError(
                "The local Converse text worker is not managed; its release cannot be verified"
            )
        if self.state.record.lease_id:
            raise ValueError("A resource lease is already active; inspect or restore it first")
        required = payload.get("required_bytes", 0)
        if type(required) is not int or not self.minimum <= required <= 1 << 46:
            raise ValueError("Declare required local headroom in bytes")
        required_gpu = payload.get("required_gpu_bytes", 0)
        if type(required_gpu) is not int or not 0 <= required_gpu <= 1 << 46:
            raise ValueError("Invalid requested GPU headroom")
        if required_gpu and self.gpu_probe is None:
            raise ValueError("No managed GPU measurement supports the requested VRAM headroom")
        gpu_before = self.gpu_probe() if self.gpu_probe is not None else None
        restore_gpu = self.state.record.restore_gpu_bytes
        if self.gpu_probe is not None:
            if gpu_before is None or gpu_before["worker_resident_bytes"] <= 0:
                raise ValueError("Managed GPU residency is unavailable; handoff refused")
            if required_gpu > gpu_before["total_bytes"]:
                raise ValueError("Requested GPU headroom exceeds the configured device capacity")
            restore_gpu = max(
                restore_gpu,
                self.gpu_restore_reserve,
                gpu_before["worker_resident_bytes"] + self.minimum,
            )
        task_id = payload.get("task_id")
        if task_id and self.task_status:
            task = await self.task_status(task_id)
            if task.get("state") in {"succeeded", "failed", "cancelled", "paused"}:
                raise ValueError("The bound task is inactive; no resource handoff was started")
        await self._save(
            phase="quiescing",
            reason=payload.get("reason", "Explicit local work handoff"),
            restore_gpu_bytes=restore_gpu,
        )
        await self.converse.enabled(False)
        await self._save(phase="unloading")
        workers = self.manager.status()["workers"]
        whistle = workers.get("whistle", {})
        whistle_stop = whistle.get("stop_evidence") or {}
        whistle_released = (
            whistle.get("state") == "stopped"
            and not whistle.get("ready")
            and whistle_stop.get("reaped")
            and whistle_stop.get("group_empty")
        )
        # A canceled Whistle request can already have released its process.
        # That is zero current residency, not an unknown running allocation.
        # Still require fresh manager/group reconciliation below. Missing RSS
        # for every other worker remains an admission refusal.
        measured = [
            0 if worker_id == "whistle" and whistle_released else row.get("group_rss_bytes")
            for worker_id, row in workers.items()
        ]
        if any(value is None for value in measured):
            raise RuntimeError("Worker resident memory is unavailable; admission refused")
        await self._save(
            restore_bytes=max(self.state.record.restore_bytes, sum(measured) + self.minimum)
        )
        stopped = await self._stop_workers()
        if any(
            item.get("stop_evidence", {}).get("rss_before_bytes") is None
            and not (
                item.get("worker_id") == "whistle"
                and whistle_released
                and item.get("release_confirmed")
            )
            for item in stopped
        ):
            raise RuntimeError("Resident-memory release was not measurable; admission refused")
        available = self.memory_probe()
        if available is None or available < required:
            raise RuntimeError("Insufficient measured local headroom; admission refused")
        if self.gpu_probe is not None:
            gpu_after = self.gpu_probe()
            if gpu_after is None or gpu_after["worker_resident_bytes"] != 0:
                raise RuntimeError("GPU release measurement unavailable; admission refused")
            self.gpu_release = {
                "before": gpu_before,
                "after": gpu_after,
                "used_bytes_decrease": gpu_before["used_bytes"] - gpu_after["used_bytes"],
                "limits": "Driver samples can race other workloads; verified worker group is reaped",
            }
            if gpu_after["free_bytes"] < required_gpu:
                raise RuntimeError("Insufficient measured GPU headroom; admission refused")
        if task_id and self.task_status:
            task = await self.task_status(task_id)
            if task.get("state") in {"succeeded", "failed", "cancelled", "paused"}:
                await self._restore_locked()
                return
        await self._save(
            phase="local_work",
            required_bytes=required,
            required_gpu_bytes=required_gpu,
            lease_id=new_id(),
            lease_task_id=task_id,
        )

    async def _restore_locked(self):
        if not self.intent.read().enabled:
            await self._off_locked()
            return
        await self._save(phase="restoring")
        available = self.memory_probe()
        if available is None or available < max(
            self.minimum, self.restore_reserve, self.state.record.restore_bytes
        ):
            raise RuntimeError("Restoration lacks measured minimum headroom")
        if self.gpu_probe is not None:
            gpu = self.gpu_probe()
            if (
                gpu is None
                or gpu["free_bytes"]
                < max(
                    0,
                    self.gpu_restore_reserve,
                    self.state.record.restore_gpu_bytes,
                )
                - gpu["worker_resident_bytes"]
            ):
                raise RuntimeError("Restoration lacks measured GPU headroom")
        for worker_id in self.manager.status()["workers"]:
            if not self.intent.read().enabled:
                await self._off_locked()
                return
            if self.before_start:
                await self.before_start(worker_id)
            result = await self.manager.start(worker_id)
            if not result.get("ok") or not result.get("ready"):
                raise RuntimeError("Managed conversation worker did not become ready")
        if not self.intent.read().enabled:
            await self._off_locked()
            return
        await self.converse.enabled(True)
        if not self.intent.read().enabled:
            await self._off_locked()
            return
        await self._save(
            phase="ready",
            lease_id=None,
            lease_task_id=None,
            required_bytes=0,
            required_gpu_bytes=0,
            reason="Managed APIs ready; voice weights load on demand",
        )

    async def close(self):
        failure = None
        try:
            await self.manager.close()
            self.state.save(
                phase="initializing",
                reason="Managed voice stopped; awaiting resource service startup",
            )
        except Exception as exc:
            failure = exc
            self.state.save(phase="failed", reason="Resource shutdown could not confirm release")
        finally:
            self.db.close()
            self.state.close()
            await self.converse.close()
            if self.journal and hasattr(self.journal, "close"):
                await self.journal.close()
        if failure:
            raise failure
