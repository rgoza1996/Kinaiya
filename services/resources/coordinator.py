"""Explicit measured conversation-worker handoff; core services stay independent."""

import asyncio
import hashlib
import inspect
import json
import os
import sqlite3
import stat

from packages.contracts.models import AppendRequest, new_id
from packages.contracts.standing_work import StandingConflict, StandingUnavailable
from packages.resources.processes import _mem_available_bytes
from services.converse.intent import ConverseIntent, OwnerIntentConflict
from services.resources.state import PROFILES


class WhistleAdmissionError(RuntimeError):
    def __init__(self, *, cleanup_confirmed=False):
        super().__init__("Whistle resource admission is unavailable")
        self.cleanup_confirmed = cleanup_confirmed


class Qwen3AdmissionError(RuntimeError):
    def __init__(self, *, cleanup_confirmed=False):
        super().__init__("Qwen3 resource admission is unavailable")
        self.cleanup_confirmed = cleanup_confirmed


class TextAdmissionError(Qwen3AdmissionError):
    def __init__(self, *, cleanup_confirmed=False):
        super().__init__(cleanup_confirmed=cleanup_confirmed)
        self.args = ("Managed text resource admission is unavailable",)


class ResourceCoordinator:
    def __init__(
        self,
        manager,
        state,
        converse,
        *,
        journal=None,
        handoff_journal=None,
        task_status=None,
        memory_probe=_mem_available_bytes,
        minimum_free_bytes=128 * 1024 * 1024,
        local_converse_unmanaged=False,
        before_start=None,
        restore_reserve_bytes=0,
        whistle_restore_reserve_bytes=0,
        qwen3_restore_reserve_bytes=0,
        text_worker_id="qwen3",
        text_restore_reserve_bytes=None,
        managed_text_binding=None,
        gpu_probe=None,
        gpu_restore_reserve_bytes=0,
    ):
        workers = manager.status()["workers"]
        text_workers = set(workers) & {"qwen3", "gguf"}
        if (
            type(text_worker_id) is not str
            or text_worker_id not in {"qwen3", "gguf"}
            or text_workers - {text_worker_id}
        ):
            raise ValueError("Configure exactly one selected managed text worker")
        if text_worker_id == "gguf" and (not managed_text_binding or "gguf" not in workers):
            raise ValueError("Managed GGUF requires its protected worker binding")
        if (
            managed_text_binding is not None
            and managed_text_binding.get("provider")
            != {"qwen3": "managed-qwen3", "gguf": "managed-gguf"}[text_worker_id]
        ):
            raise ValueError("Managed text binding does not match the selected worker")
        if text_restore_reserve_bytes is not None and (
            type(text_restore_reserve_bytes) is not int or text_restore_reserve_bytes < 0
        ):
            raise ValueError("Managed text restore reserve must be nonnegative bytes")
        self.manager, self.state, self.converse = manager, state, converse
        self.intent = ConverseIntent(
            state.path.parent / "converse-intent.json",
            initial_enabled=state.record.desired_converse,
        )
        self.controller_id = new_id()
        self.journal, self.task_status = journal, task_status
        self.handoff_journal = handoff_journal
        self.memory_probe, self.minimum = memory_probe, minimum_free_bytes
        self.local_converse_unmanaged = local_converse_unmanaged
        self.before_start = before_start
        self.restore_reserve = restore_reserve_bytes
        self.whistle_restore_reserve = whistle_restore_reserve_bytes
        self.qwen3_restore_reserve = qwen3_restore_reserve_bytes
        self.text_worker_id = text_worker_id
        self.text_restore_reserve = (
            qwen3_restore_reserve_bytes
            if text_restore_reserve_bytes is None
            else text_restore_reserve_bytes
        )
        self.managed_text_binding = (
            dict(managed_text_binding) if managed_text_binding is not None else None
        )
        self.gpu_probe = gpu_probe
        self.gpu_restore_reserve = gpu_restore_reserve_bytes
        self.gpu_release = None
        self.lock = asyncio.Lock()
        # Allocations never need the action/ledger lock. An owner action may
        # therefore await the broker's existing admission/cleanup owner.
        self.admission_lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._text_lifecycle = None
        self._whistle_lifecycle = None
        self._standing_composition = None
        self.publication_pending = False
        self._whistle_preemptions = 0
        self._has_whistle = "whistle" in self.manager.status()["workers"]
        self._qwen3_preemptions = 0
        self._has_qwen3 = "qwen3" in self.manager.status()["workers"]
        self._has_text = text_worker_id in workers
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
        from services.resources.standing_handoff import StandingHandoff

        self.standing_handoff = StandingHandoff(self)
        from services.resources.standing_barrier import StandingBarrier

        self.standing_barrier = StandingBarrier(self)
        self._close_owner = None

    async def bind_standing_claim(self, claim):
        return await self.standing_barrier.run("bind", claim)

    async def lookup_standing_binding(self, claim):
        return await self.standing_barrier.run("binding_lookup", claim)

    async def seal_standing_claim(self, request):
        return await self.standing_barrier.run("seal", request)

    async def lookup_standing_seal(self, request):
        return await self.standing_barrier.run("seal_lookup", request)

    def register_text_lifecycle(self, *, abort, drain, release):
        """Register trusted selected-broker hooks once; never a second owner."""
        if self._text_lifecycle is not None or self._closing or not self._has_text:
            raise ValueError("Managed text lifecycle cannot be registered")
        if not all(
            callable(hook) for hook in (abort, drain, release)
        ) or inspect.iscoroutinefunction(abort):
            raise ValueError("Managed text lifecycle requires a synchronous abort hook")
        self._text_lifecycle = abort, drain, release

    def register_whistle_lifecycle(self, *, abort, drain):
        if (
            self._whistle_lifecycle is not None
            or self._closing
            or not self._has_whistle
            or not callable(abort)
            or not callable(drain)
            or inspect.iscoroutinefunction(abort)
        ):
            raise ValueError("Whistle lifecycle cannot be registered")
        self._whistle_lifecycle = abort, drain

    def register_standing_composition(self, *, whistle=None, qwen3=None, gguf=None):
        """Capture trusted selected API owners; missing hooks refuse capability."""
        selected = whistle, qwen3, gguf
        if self._standing_composition is not None and any(
            current is not previous
            for current, previous in zip(selected, self._standing_composition, strict=True)
        ):
            raise ValueError("Resources composition cannot be replaced")
        self._standing_composition = selected

    @staticmethod
    def _same_hook(actual, expected):
        return (
            callable(actual)
            and callable(expected)
            and getattr(actual, "__self__", None) is getattr(expected, "__self__", object())
            and getattr(actual, "__func__", None) is getattr(expected, "__func__", object())
        )

    def require_standing_ownership(self):
        if (
            getattr(self.manager, "actual_ownership_supported", False) is not True
            or getattr(self.converse, "actual_ownership_supported", False) is not True
        ):
            raise StandingUnavailable
        selected_whistle, qwen, gguf = self._standing_composition or (None, None, None)
        if qwen is not None and gguf is not None:
            raise StandingUnavailable
        if self._has_text or qwen is not None or gguf is not None:
            hooks = self._text_lifecycle
            if hooks is None:
                raise StandingUnavailable
            broker = getattr(hooks[0], "__self__", None)
            endpoint = getattr(broker, "endpoint", None)
            selected = qwen if qwen is not None else gguf
            if (
                selected is not None
                and broker is not selected
                or getattr(broker, "actual_ownership_supported", False) is not True
                or getattr(endpoint, "actual_ownership_supported", False) is not True
                or not self._same_hook(hooks[0], getattr(broker, "abort_active", None))
                or not self._same_hook(hooks[1], getattr(broker, "drain", None))
                or not self._same_hook(hooks[2], getattr(endpoint, "release_after_stop", None))
            ):
                raise StandingUnavailable
        if selected_whistle is not None or self._whistle_lifecycle is not None:
            hooks = self._whistle_lifecycle
            if hooks is None:
                raise StandingUnavailable
            broker = getattr(hooks[0], "__self__", None)
            if (
                selected_whistle is not None
                and broker is not selected_whistle
                or getattr(broker, "actual_ownership_supported", False) is not True
                or getattr(getattr(broker, "endpoint", None), "actual_ownership_supported", False)
                is not True
                or not self._same_hook(hooks[0], getattr(broker, "abort_active", None))
                or not self._same_hook(hooks[1], getattr(broker, "drain", None))
            ):
                raise StandingUnavailable

    async def stop_text(self, identity=None):
        if identity is not None and identity.worker_id != self.text_worker_id:
            raise ValueError("Cleanup identity is not the selected text worker")
        captured = identity or self.manager.owned_identity(self.text_worker_id)
        result = (
            await self.manager.stop(self.text_worker_id)
            if identity is None
            else await self.manager.stop_if_current(identity)
        )
        confirmed = self._native_stopped(result)
        if result.get("ok") and result.get("already"):
            row = self.manager.status()["workers"][self.text_worker_id]
            proof = row.get("stop_evidence") or {}
            if row.get("state") == "stopped" and row.get("owned_pgid") is None:
                if proof.get("reaped") and proof.get("group_empty"):
                    result = {**result, "release_confirmed": True, "stop_evidence": proof}
                    confirmed = True
                elif row.get("owned_pid") is None and not proof:
                    # No process was granted. Endpoint startup leases must
                    # independently agree before this can confirm release.
                    confirmed = True
        if not confirmed:
            raise RuntimeError("Managed text process release is unconfirmed")
        if self._text_lifecycle is not None:
            release = self._text_lifecycle[2]
            if await release(captured, result) is not True:
                raise RuntimeError("Managed text endpoint release is unconfirmed")
        return result

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
        handoff_ready = False
        try:
            await self.standing_handoff.register()
            try:
                await self.standing_barrier.register()
            except StandingUnavailable:
                pass
            handoff_ready = True
        except StandingUnavailable:
            # A failed task gateway never disables independent owner cleanup.
            pass
        async with self.lock:
            if not handoff_ready or self.standing_handoff.retained:
                await self._save(
                    phase="failed",
                    reason="Retained resource handoff ownership is unconfirmed; inspect before restoring",
                )
                return
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
                if self.managed_text_binding is not None and self.local_converse_unmanaged:
                    raise RuntimeError("Selected managed text health does not match its binding")
                await self._save(phase="initializing")
                if self.intent.read().enabled:
                    await self._restore_locked()
                else:
                    await self._off_locked()
            except Exception:
                await self._save(
                    phase="failed", reason="Resource startup failed; inspect worker status"
                )

    def _provider_is_unmanaged(self, health):
        local = health.get("provider", {}).get("local")
        if type(local) is not bool:
            raise RuntimeError("Converse provider locality is unavailable; handoff refused")
        expected = self.managed_text_binding
        if expected is None:
            return local
        binding, provider = health.get("managed_text"), health.get("provider", {})
        # Protected startup configuration identifies the selected profile, not
        # a ready process. Only the manager can prove residency and release.
        matches = (
            self._has_text
            and type(binding) is dict
            and binding.keys() == expected.keys()
            and all(
                type(binding[key]) is type(value) and binding[key] == value
                for key, value in expected.items()
            )
            and local is True
            and provider.get("provider") == expected["provider"]
            and provider.get("model") == expected["model"]
            and provider.get("streaming") is True
        )
        # A changed cloud/local profile must not inherit the configured Qwen
        # worker's release authority either.
        return not matches

    def _provider_locality(self, health):
        self.local_converse_unmanaged = self._provider_is_unmanaged(health)

    async def action(self, payload):
        if payload.get("operation") == "handoff" and payload.get("task_id") is not None:
            return await self.standing_handoff.run(payload, self._registered_action)
        return await self._registered_action(payload)

    async def _registered_action(self, payload, *, capture=None):
        bound = payload.get("operation") == "handoff" and payload.get("task_id") is not None
        if bound:
            self.standing_handoff.require(payload, capture)
        # A new authenticated off/handoff request must beat a Whistle restart
        # already awaiting readiness under the lifecycle lock. A replay/conflict
        # must not revoke legitimate current work. The existing durable action
        # implementation remains the authority.
        preempt = (self._has_whistle or self._has_text) and (
            payload.get("operation") == "handoff"
            or (
                payload.get("operation") == "converse"
                and payload.get("enabled") is False
                and payload.get("expected_owner_intent_revision") is None
            )
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
            if bound:
                self.standing_handoff.require(payload, capture)
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
            if bound:
                capture.begin_effects()
            self._whistle_preemptions += int(self._has_whistle)
            self._qwen3_preemptions += int(self._has_text)
        try:
            if bound:
                return await self._action(payload, capture=capture)
            return await self._action(payload)
        finally:
            if preempt:
                self._whistle_preemptions -= int(self._has_whistle)
                self._qwen3_preemptions -= int(self._has_text)

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
            if self._provider_is_unmanaged(health):
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
            self._closing
            or self._whistle_preemptions
            or record.phase != "ready"
            or record.lease_id
            or not record.desired_converse
            or not choice.enabled
            or record.owner_intent_revision != choice.revision
        ):
            raise WhistleAdmissionError(cleanup_confirmed=True)
        return record.revision, choice.revision

    @staticmethod
    def _native_stopped(result):
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
        async with self.admission_lock:
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
                        cleanup_confirmed=self._native_stopped(stopped)
                    ) from exc
                identity = self.manager.owned_identity("whistle")
                if not result.get("ok") or not result.get("ready") or identity is None:
                    stopped = await self.manager.stop("whistle")
                    raise WhistleAdmissionError(cleanup_confirmed=self._native_stopped(stopped))
            try:
                if self.whistle_policy() != expected_policy:
                    raise WhistleAdmissionError()
            except (OSError, ValueError, WhistleAdmissionError) as exc:
                stopped = await self.manager.stop_if_current(identity)
                raise WhistleAdmissionError(
                    cleanup_confirmed=self._native_stopped(stopped)
                ) from exc
            return identity

    def text_policy(self):
        record, choice = self.state.record, self.intent.read()
        if (
            self._closing
            or not self._has_text
            or (self.text_worker_id == "gguf" and self._text_lifecycle is None)
            or self._qwen3_preemptions
            or record.phase != "ready"
            or record.lease_id
            or not record.desired_converse
            or not choice.enabled
            or record.owner_intent_revision != choice.revision
        ):
            raise TextAdmissionError(cleanup_confirmed=True)
        return record.revision, choice.revision

    def qwen3_policy(self):
        return self.text_policy()

    async def _text_admission_cleanup(self, identity=None):
        try:
            await self.stop_text(identity)
        except Exception:
            return False
        return True

    async def admit_qwen3(self, expected_policy):
        return await self.admit_text(expected_policy)

    async def resident_text(self, expected_policy):
        """Capture existing GGUF ownership without allocation or a cold start."""
        async with self.admission_lock:
            if self.text_worker_id != "gguf" or self.text_policy() != expected_policy:
                raise TextAdmissionError(cleanup_confirmed=True)
            identity = self.manager.owned_identity(self.text_worker_id)
            if identity is None or not self.manager.is_current(identity):
                raise TextAdmissionError(cleanup_confirmed=True)
            return identity

    async def admit_text(self, expected_policy):
        """Admit one fixed child; recheck revocation after awaited readiness.

        The broker retains this operation through request cancellation. A new
        request may restore a released allocation; it never retries generation.
        """
        async with self.admission_lock:
            if self.text_policy() != expected_policy:
                raise TextAdmissionError(cleanup_confirmed=True)
            worker_id = self.text_worker_id
            identity = self.manager.owned_identity(worker_id)
            if identity is None:
                if self._text_lifecycle is not None and not await self._text_admission_cleanup():
                    raise TextAdmissionError()
                if self.text_policy() != expected_policy:
                    raise TextAdmissionError(cleanup_confirmed=True)
                if self.before_start:
                    try:
                        await self.before_start(worker_id)
                    except Exception as exc:
                        raise TextAdmissionError(cleanup_confirmed=True) from exc
                if self.text_policy() != expected_policy:
                    raise TextAdmissionError(cleanup_confirmed=True)
                available = self.memory_probe()
                if available is None or available < max(self.minimum, self.text_restore_reserve):
                    raise TextAdmissionError(cleanup_confirmed=True)
                try:
                    result = await self.manager.start(worker_id)
                except asyncio.CancelledError:
                    await self._text_admission_cleanup()
                    raise
                except Exception as exc:
                    raise TextAdmissionError(
                        cleanup_confirmed=await self._text_admission_cleanup()
                    ) from exc
                identity = self.manager.owned_identity(worker_id)
                if not result.get("ok") or not result.get("ready") or identity is None:
                    raise TextAdmissionError(cleanup_confirmed=await self._text_admission_cleanup())
            try:
                if self.text_policy() != expected_policy:
                    raise TextAdmissionError()
            except (OSError, ValueError, TextAdmissionError) as exc:
                raise TextAdmissionError(
                    cleanup_confirmed=await self._text_admission_cleanup(identity)
                ) from exc
            return identity

    async def _action(self, payload, *, capture=None):
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
            if operation == "handoff" and payload.get("task_id") is not None:
                self.standing_handoff.require(payload, capture)
            with self.db:
                self.db.execute("INSERT INTO actions VALUES(?,?,NULL)", (key, digest))
            intent_change = None
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
                    if payload.get("task_id") is not None:
                        await self._handoff_locked(payload, capture=capture)
                    else:
                        await self._handoff_locked(payload)
                elif operation == "restore":
                    await self._restore_locked()
                elif operation == "converse":
                    desired = payload["enabled"]
                    choice = self.intent.set(
                        desired,
                        expected_revision=payload.get("expected_owner_intent_revision"),
                    )
                    intent_change = {"enabled": choice.enabled, "revision": choice.revision}
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
                if intent_change is not None:
                    result["owner_intent_change"] = intent_change
            except asyncio.CancelledError:
                self.state.save(
                    phase="failed", reason="Resource action interrupted; inspect status"
                )
                raise
            except (StandingConflict, StandingUnavailable):
                raise
            except OwnerIntentConflict as exc:
                observed = exc.observed
                result = {
                    "ok": False,
                    "owner_intent_conflict": True,
                    "error": "Owner intent revision conflict",
                    "observed_owner_intent": {
                        "enabled": observed.enabled,
                        "revision": observed.revision,
                    },
                    "policy": {
                        **self.state.record.model_dump(),
                        "desired_converse": observed.enabled,
                        "owner_intent_revision": observed.revision,
                    },
                }
            except ValueError as exc:
                result = {
                    "ok": False,
                    "error": str(exc)[:160],
                    "policy": self.state.record.model_dump(),
                }
                if intent_change is not None:
                    result["owner_intent_change"] = intent_change
            except Exception as exc:
                await self._save(phase="failed", reason="Resource action failed; inspect status")
                result = {
                    "ok": False,
                    "error": str(exc)[:160],
                    "policy": self.state.record.model_dump(),
                }
                if intent_change is not None:
                    result["owner_intent_change"] = intent_change
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
        workers = tuple(self.manager.status()["workers"])
        hooks, failure, first_identity, first_result = self._text_lifecycle, None, None, None
        if self._whistle_lifecycle is not None:
            try:
                self._whistle_lifecycle[0]()
            except (Exception, asyncio.CancelledError) as exc:
                failure = exc
        if hooks is not None:
            try:
                hooks[0]()
            except (Exception, asyncio.CancelledError) as exc:
                failure = exc
        # First stop unblocks real native I/O. It is not the release barrier:
        # an admitted start can still be delayed before entering Manager.
        for worker_id in workers:
            try:
                if worker_id == self.text_worker_id:
                    first_identity = self.manager.owned_identity(worker_id)
                stopped = await self.manager.stop(worker_id)
                if worker_id == self.text_worker_id:
                    first_result = stopped
            except (Exception, asyncio.CancelledError) as exc:
                failure = failure or exc
        if hooks is not None:
            try:
                await hooks[1]()
            except (Exception, asyncio.CancelledError) as exc:
                failure = failure or exc
        if self._whistle_lifecycle is not None:
            try:
                await self._whistle_lifecycle[1]()
            except (Exception, asyncio.CancelledError) as exc:
                failure = failure or exc
        try:
            # Legacy Qwen/Whistle admissions have no broker hook. This also
            # joins their allocation transaction before the final stop. No
            # admission lock is held while draining broker-owned work above.
            async with self.admission_lock:
                pass
        except asyncio.CancelledError as exc:
            failure = failure or exc
        if hooks is not None and first_identity is not None and first_result is not None:
            try:
                if await hooks[2](first_identity, first_result) is not True:
                    raise RuntimeError("Managed text endpoint release is unconfirmed")
            except (Exception, asyncio.CancelledError) as exc:
                failure = failure or exc
        results = []
        for worker_id in workers:
            result = (
                await self.stop_text()
                if worker_id == self.text_worker_id
                else await self.manager.stop(worker_id)
            )
            if not result.get("ok"):
                raise RuntimeError("Managed worker termination is unconfirmed")
            evidence = result.get("stop_evidence")
            if evidence is not None and (
                not evidence.get("reaped") or not evidence.get("group_empty")
            ):
                raise RuntimeError("Managed worker group release is unconfirmed")
            if result.get("already"):
                row = self.manager.status()["workers"][worker_id]
                retained = row.get("stop_evidence") or {}
                if (
                    row.get("state") == "stopped"
                    and row.get("owned_pgid") is None
                    and retained.get("reaped")
                    and retained.get("group_empty")
                ):
                    result = {**result, "release_confirmed": True, "stop_evidence": retained}
            results.append(result)
        if failure is not None:
            raise failure
        return results

    async def _handoff_locked(self, payload, *, capture=None):
        bound = payload.get("task_id") is not None
        if bound:
            self.standing_handoff.require(payload, capture)
        if self.state.record.profile == "conversation_priority":
            raise ValueError(
                "Conversation priority queues demanding local work; select local work priority for handoff"
            )
        self._provider_locality(await self.converse.health())
        if bound:
            self.standing_handoff.require(payload, capture)
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
            if bound:
                self.standing_handoff.require(payload, capture)
            if task.get("state") in {"succeeded", "failed", "cancelled", "paused"}:
                raise ValueError("The bound task is inactive; no resource handoff was started")
        if bound:
            capture.begin_effects()
        await self._save(
            phase="quiescing",
            reason=payload.get("reason", "Explicit local work handoff"),
            restore_gpu_bytes=restore_gpu,
        )
        if bound:
            self.standing_handoff.require(payload, capture)
        await self.converse.enabled(False)
        if bound:
            self.standing_handoff.require(payload, capture)
        await self._save(phase="unloading")
        if bound:
            self.standing_handoff.require(payload, capture)
        workers = self.manager.status()["workers"]
        released = set()
        for worker_id in ("whistle", self.text_worker_id):
            row = workers.get(worker_id, {})
            proof = row.get("stop_evidence") or {}
            if (
                row.get("state") == "stopped"
                and not row.get("ready")
                and row.get("owned_pgid") is None
                and proof.get("reaped")
                and proof.get("group_empty")
            ):
                released.add(worker_id)
        # A canceled fixed native request can already have released its process.
        # That is zero current residency, not an unknown running allocation.
        # Still require fresh manager/group reconciliation below. Missing RSS
        # for every other worker remains an admission refusal.
        measured = [
            0 if worker_id in released else row.get("group_rss_bytes")
            for worker_id, row in workers.items()
        ]
        if any(value is None for value in measured):
            raise RuntimeError("Worker resident memory is unavailable; admission refused")
        await self._save(
            restore_bytes=max(self.state.record.restore_bytes, sum(measured) + self.minimum)
        )
        if bound:
            self.standing_handoff.require(payload, capture)
        stopped = await self._stop_workers()
        if bound:
            self.standing_handoff.require(payload, capture)
        if any(
            item.get("stop_evidence", {}).get("rss_before_bytes") is None
            and not (item.get("worker_id") in released and item.get("release_confirmed"))
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
            if bound:
                self.standing_handoff.require(payload, capture)
            if task.get("state") in {"succeeded", "failed", "cancelled", "paused"}:
                await self._restore_locked()
                return
        await self._save(
            phase="local_work",
            required_bytes=required,
            required_gpu_bytes=required_gpu,
            lease_id=capture.request.lease_id
            if bound and capture.request is not None
            else new_id(),
            lease_task_id=task_id,
        )

    async def _restore_locked(self):
        if self._closing or not self.intent.read().enabled:
            await self._off_locked()
            return
        await self._save(phase="restoring")
        async with self.admission_lock:
            restored = await self._restore_allocations()
        # Stop/drain can await admission. Never retain admission_lock here.
        if not restored or self._closing or not self.intent.read().enabled:
            await self._off_locked()
            return
        if self.managed_text_binding is not None:
            self._provider_locality(await self.converse.health())
            if self.local_converse_unmanaged:
                raise RuntimeError("Selected managed text health does not match its binding")
        await self.converse.enabled(True)
        if self._closing or not self.intent.read().enabled:
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

    async def _restore_allocations(self):
        """Run under admission_lock, including the resident credit snapshot."""
        if self._has_text and self.text_worker_id == "gguf" and self._text_lifecycle is None:
            raise RuntimeError("Managed GGUF lifecycle is not registered")
        reserve, recorded = self.restore_reserve, self.state.record.restore_bytes
        credited_text = self.manager.owned_identity(self.text_worker_id) if self._has_text else None
        if credited_text is not None:
            # An existing verified text child is already included in the fresh
            # host measurement. Reopening Converse must not reserve its whole
            # startup allocation a second time. Other worker rules are unchanged.
            reserve = max(0, reserve - self.text_restore_reserve)
            resident = self.manager.status()["workers"][self.text_worker_id].get("group_rss_bytes")
            if type(resident) is int and resident >= 0:
                recorded = max(0, recorded - resident)
        available = self.memory_probe()
        if available is None or available < max(self.minimum, reserve, recorded):
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
            if self._closing or not self.intent.read().enabled:
                return False
            if self.before_start:
                await self.before_start(worker_id)
            if self._closing or not self.intent.read().enabled:
                return False
            if worker_id == self.text_worker_id:
                if credited_text is not None and self.manager.is_current(credited_text):
                    # Reuse that exact credited incarnation without calling
                    # start(), which could replace an exited leader after an
                    # await. Later loss is handled by ordinary fresh admission.
                    continue
                if self._text_lifecycle is not None:
                    await self.stop_text()
                if self._closing or not self.intent.read().enabled:
                    return False
                needed = max(self.minimum, self.text_restore_reserve)
                if credited_text is not None:
                    # The earlier credit no longer applies. A replacement
                    # requires a fresh full restore observation, not the old
                    # discounted measurement from before awaited work.
                    needed = max(needed, self.restore_reserve, self.state.record.restore_bytes)
                available = self.memory_probe()
                if available is None or available < needed:
                    raise RuntimeError("Text restoration lacks measured minimum headroom")
            try:
                result = await self.manager.start(worker_id)
            except (Exception, asyncio.CancelledError):
                if worker_id == self.text_worker_id:
                    await self._text_admission_cleanup()
                raise
            if not result.get("ok") or not result.get("ready"):
                if worker_id == self.text_worker_id:
                    await self._text_admission_cleanup()
                raise RuntimeError("Managed conversation worker did not become ready")
        return not self._closing and self.intent.read().enabled

    async def _close_owned(self):
        from packages.contracts.operation_ownership import ActualOperation, actual_owner

        cleanup = ActualOperation(lambda _: None)
        root = object()
        cleanup.hold(root)
        retain = getattr(self.manager, "retain_operations", None)
        if callable(retain):
            retain(cleanup)
        with actual_owner(cleanup):
            try:
                await self._retire_owned(cleanup, root)
            finally:
                cleanup.release(root)

    async def _retire_owned(self, cleanup, root):
        self._closing = True
        self.standing_barrier.shutdown()
        await self.standing_handoff.close()
        # Never join capture owners under the controller lock they may need.
        # This waits for settlement, including failed cleanup; Manager.close
        # still performs its independent exact-owned retirement/retry below.
        await self.standing_barrier.join_shutdown()
        while self.standing_handoff.native:
            await asyncio.wait(tuple(self.standing_handoff.native))
        async with self.lock:
            if self._closed:
                return
            failure = None
            try:
                if self._text_lifecycle is not None or self._whistle_lifecycle is not None:
                    await self._stop_workers()
            except (Exception, asyncio.CancelledError) as exc:
                failure = exc
            finally:
                try:
                    await self.manager.close()
                except (Exception, asyncio.CancelledError) as exc:
                    failure = failure or exc
        # Join actual cleanup outside the controller lock, including a child
        # that was already waiting to enter it when shutdown fenced admission.
        # Manager retirement/retry above is independent of a failed proof.
        cleanup.release(root)
        await cleanup.join()
        if cleanup.failed:
            failure = failure or StandingUnavailable()
        async with self.lock:
            try:
                if failure is None:
                    self.state.save(
                        phase="initializing",
                        reason="Managed voice stopped; awaiting resource service startup",
                    )
                else:
                    self.state.save(
                        phase="failed", reason="Resource shutdown could not confirm release"
                    )
            finally:
                self._closed = True
                for retire in (self.standing_handoff._maybe_close, self.db.close, self.state.close):
                    try:
                        retire()
                    except (Exception, asyncio.CancelledError) as exc:
                        failure = failure or exc
                clients = [self.converse, self.journal]
                if self.handoff_journal is not self.journal:
                    clients.append(self.handoff_journal)
                for client in clients:
                    if client is not None and callable(getattr(client, "close", None)):
                        try:
                            await client.close()
                        except (Exception, asyncio.CancelledError) as exc:
                            failure = failure or exc
            if failure is not None:
                raise failure

    async def close(self):
        self._closing = True
        self.standing_barrier.shutdown()
        if self._close_owner is None:
            self._close_owner = asyncio.create_task(self._close_owned())
            self._close_owner.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        done, _ = await asyncio.wait({self._close_owner}, timeout=1)
        if not done:
            raise StandingUnavailable
        return self._close_owner.result()
