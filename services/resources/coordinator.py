"""Explicit measured conversation-worker handoff; core services stay independent."""

import asyncio
import hashlib
import json
import os
import sqlite3
import stat

from packages.contracts.models import AppendRequest, new_id
from packages.resources.processes import _mem_available_bytes
from services.resources.state import PROFILES


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
        gpu_probe=None,
        gpu_restore_reserve_bytes=0,
    ):
        self.manager, self.state, self.converse = manager, state, converse
        self.controller_id = new_id()
        self.journal, self.task_status = journal, task_status
        self.memory_probe, self.minimum = memory_probe, minimum_free_bytes
        self.local_converse_unmanaged = local_converse_unmanaged
        self.before_start = before_start
        self.restore_reserve = restore_reserve_bytes
        self.gpu_probe = gpu_probe
        self.gpu_restore_reserve = gpu_restore_reserve_bytes
        self.gpu_release = None
        self.lock = asyncio.Lock()
        self.publication_pending = False
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
        return {
            "schema_version": "1.0",
            "controller_id": self.controller_id,
            "policy": self.state.record.model_dump(),
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
                self.local_converse_unmanaged = bool(health.get("provider", {}).get("local", False))
                desired = self.state.record.desired_converse and health.get("enabled") is True
                await self._save(phase="initializing", desired_converse=desired)
                if desired:
                    await self._restore_locked()
                else:
                    await self._save(phase="off")
            except Exception:
                await self._save(
                    phase="failed", reason="Resource startup failed; inspect worker status"
                )

    async def action(self, payload):
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
                    await self._save(desired_converse=desired)
                    if desired:
                        await self._restore_locked()
                    else:
                        await self.converse.enabled(False)
                        await self._stop_workers()
                        await self._save(
                            phase="off",
                            lease_id=None,
                            lease_task_id=None,
                            required_bytes=0,
                            required_gpu_bytes=0,
                        )
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
            results.append(result)
        return results

    async def _handoff_locked(self, payload):
        if self.state.record.profile == "conversation_priority":
            raise ValueError(
                "Conversation priority queues demanding local work; select local work priority for handoff"
            )
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
        health = await self.converse.health()
        await self._save(
            desired_converse=self.state.record.desired_converse and health.get("enabled") is True
        )
        await self.converse.enabled(False)
        await self._save(phase="unloading")
        measured = [row.get("group_rss_bytes") for row in self.manager.status()["workers"].values()]
        if any(value is None for value in measured):
            raise RuntimeError("Worker resident memory is unavailable; admission refused")
        await self._save(
            restore_bytes=max(self.state.record.restore_bytes, sum(measured) + self.minimum)
        )
        stopped = await self._stop_workers()
        if any(item.get("stop_evidence", {}).get("rss_before_bytes") is None for item in stopped):
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
        if not self.state.record.desired_converse:
            await self._save(
                phase="off",
                lease_id=None,
                lease_task_id=None,
                required_bytes=0,
                required_gpu_bytes=0,
            )
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
            if self.before_start:
                await self.before_start(worker_id)
            result = await self.manager.start(worker_id)
            if not result.get("ok") or not result.get("ready"):
                raise RuntimeError("Managed conversation worker did not become ready")
        await self.converse.enabled(True)
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
