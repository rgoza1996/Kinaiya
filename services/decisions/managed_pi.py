"""Owner-selected Pi attachment and lifetime for Decisions."""

import asyncio
import secrets

import httpx

from packages.agent_adapters.external_submission import PiSubmission
from packages.agent_adapters.pi_sdk import (
    PiCommandUnconfirmed,
    PiEventReplayExpired,
    PiPeerUnavailable,
    PiSdkPeer,
)
from packages.contracts.external_agent import (
    ExternalAgentBind,
    ExternalAgentCapabilities,
    ExternalAgentClaim,
    ExternalAgentCommandReceipt,
    ExternalAgentDisconnectReceipt,
    ExternalAgentGrantLookup,
    ExternalAgentRefusal,
    ExternalAgentSource,
    ExternalAgentStatus,
    ExternalAgentSubmit,
    ExternalAgentTarget,
)
from packages.contracts.external_agent_client import (
    ExternalAgentNativeConflict,
    ExternalAgentNativeUnavailable,
    ExternalAgentSessionClient,
)
from packages.providers.disclosure import combine_scopes, source_scope
from services.decisions.pi_observations import (
    PiObservations,
    PiObservationUnavailable,
    PiQueueConflict,
)
from services.decisions.pi_process import PiLaunchUnavailable, launch


class ManagedPi:
    def __init__(self, profile, journal, *, data_dir, protected):
        self.profile = profile
        self.authority = ExternalAgentSessionClient(journal)
        self.data_dir = data_dir
        self.protected = protected
        self._lock = asyncio.Lock()
        self._operations = set()
        self._closed = False
        self._connected = False
        self._route_after = None
        self._owned = self._peer = self._grant = None
        self._observations = None
        self._queue_wake = asyncio.Event()
        self._queue_task = None

    @staticmethod
    def _refuse(request_id, reason):
        return ExternalAgentRefusal(
            contract_version=1, request_id=request_id, kind="refusal", reason=reason
        )

    @property
    def _target(self):
        if self._grant is None:
            return None
        return ExternalAgentTarget(
            attachment_id=self._grant.attachment_id,
            backend_session_id=self._grant.backend_session_id,
        )

    async def _observed(self, request_id, target, grant):
        local = target == self._target and self._owned is not None
        alive = local and self._owned.process.returncode is None
        ready = alive and self._peer is not None and self._peer.available
        observations = PiObservations(self.data_dir, target)
        try:
            retired, tasks = await asyncio.to_thread(observations.status, live=ready)
        except ValueError:
            return self._refuse(request_id, "unavailable")
        state = "uncertain"
        if ready:
            state = "connected" if self._connected else "detached"
        elif alive:
            state = "unavailable"
        elif local or retired:
            state = "stopped"
        return ExternalAgentStatus(
            contract_version=1,
            request_id=request_id,
            kind="status",
            target=target,
            scope=grant.original.scope,
            backend="pi",
            state=state,
            tasks=tasks,
            capabilities=ExternalAgentCapabilities(
                submit=ready and self._connected,
                queue=ready and self._connected,
                steer=False,
                interrupt=False,
                event_replay=True,
                transcript_read=ready,
                approvals=False,
                artifacts=False,
                shutdown_owned=alive,
            ),
        )

    async def _lookup(self, attachment_id):
        return await self.authority.lookup_grant(
            ExternalAgentGrantLookup(
                contract_version=1,
                request_id=secrets.token_hex(16),
                operation="lookup_grant",
                attachment_id=attachment_id,
            )
        )

    async def _bound_grant(self, target):
        grant = await self._lookup(target.attachment_id)
        if grant.phase != "bound" or grant.backend_session_id != target.backend_session_id:
            raise ExternalAgentNativeConflict
        return grant

    async def status(self, request):
        async with self._lock:
            try:
                grant = await self._bound_grant(request.target)
            except ExternalAgentNativeConflict:
                return self._refuse(request.request_id, "scope_changed")
            except (ExternalAgentNativeUnavailable, PermissionError):
                return self._refuse(request.request_id, "unavailable")
            return await self._observed(request.request_id, request.target, grant)

    async def _perform(self, action, request, **options):
        if self._closed or len(self._operations) >= 8:
            return self._refuse(request.request_id, "unavailable")
        task = asyncio.create_task(action(request, **options))
        self._operations.add(task)

        def finished(done):
            self._operations.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def attach(self, request):
        return await self._perform(self._attach, request)

    async def _activate(self, scope):
        # A new connection starts with new user input. Unprocessed history
        # cannot become work merely because a destination is attached later.
        latest = await self.authority.journal.recent(session_id=scope.session_id, limit=1)
        self._route_after = latest[-1].sequence if latest else 0
        self._connected = True
        if self._queue_task is None:
            self._queue_task = asyncio.create_task(self._run_queue())
        self._queue_wake.set()

    def routing_target(self, source):
        if (
            self._closed
            or not self._connected
            or self._route_after is None
            or source.sequence <= self._route_after
            or self._peer is None
            or not self._peer.available
        ):
            return None
        scope = self._grant.original.scope
        if (source.session_id, source.thread_id, source.epoch) != (
            scope.session_id,
            scope.thread_id,
            scope.epoch,
        ):
            return None
        return self._target

    async def _attach(self, request):
        async with self._lock:
            if self._closed or self.profile is None:
                return self._refuse(request.request_id, "unavailable")
            try:
                grant = await self._lookup(request.attachment_id)
            except ExternalAgentNativeConflict:
                return self._refuse(request.request_id, "scope_changed")
            except (ExternalAgentNativeUnavailable, PermissionError):
                return self._refuse(request.request_id, "unavailable")
            if grant.original.profile_id != self.profile.profile_id or (
                self.profile.allow_network
                and grant.original.allowed_content != ("public_synthetic",)
            ):
                return self._refuse(request.request_id, "unauthorized")
            if self._owned is not None and self._owned.process.returncode is None:
                if self._grant is None or grant.attachment_id != self._grant.attachment_id:
                    return self._refuse(request.request_id, "busy")
                if self._grant.backend_session_id != grant.backend_session_id:
                    return self._refuse(request.request_id, "scope_changed")
                if self._peer is None or not self._peer.available:
                    return self._refuse(request.request_id, "unavailable")
                if not self._connected:
                    try:
                        await self._activate(grant.original.scope)
                    except (httpx.HTTPError, OSError, ValueError):
                        return self._refuse(request.request_id, "unavailable")
                return await self._observed(request.request_id, self._target, grant)
            if self._owned is not None:
                await self._reap()
            self._owned = self._peer = self._grant = None
            self._connected = False
            self._observations = None
            expected = grant.backend_session_id or grant.original.expected_backend_session_id
            try:
                self._owned = await launch(
                    self.profile,
                    attachment_id=grant.attachment_id,
                    data_dir=self.data_dir,
                    protected=self.protected,
                    expected_session_id=expected,
                )
                process = self._owned.process
                self._peer = await PiSdkPeer.attach(
                    process.stdout,
                    process.stdin,
                    expected_session_id=expected,
                    transcript_reader=self._read_transcript,
                )
                self._grant = await self.authority.bind(
                    ExternalAgentBind(
                        contract_version=1,
                        request_id=secrets.token_hex(16),
                        operation="bind",
                        target=ExternalAgentTarget(
                            attachment_id=grant.attachment_id,
                            backend_session_id=self._peer.session_id,
                        ),
                    )
                )
                if process.returncode is not None:
                    raise PiLaunchUnavailable
                observations = PiObservations(self.data_dir, self._target)
                await asyncio.to_thread(observations.start)
                self._observations = observations
                self._peer.retain_observations(observations, wake=self._queue_wake.set)
                await self._activate(self._grant.original.scope)
            except (
                ExternalAgentNativeConflict,
                ExternalAgentNativeUnavailable,
                httpx.HTTPError,
                OSError,
                PiPeerUnavailable,
                PiLaunchUnavailable,
                ValueError,
            ):
                if self._owned is not None:
                    await self._reap()
                return self._refuse(request.request_id, "unavailable")
            return await self._observed(request.request_id, self._target, self._grant)

    async def _read_transcript(self, **position):
        grant = self._grant
        if grant is None:
            raise ExternalAgentNativeUnavailable
        return await self.authority.read_bound_transcript(grant, **position)

    async def submit(self, request, *, routing_source=None):
        return await self._perform(self._submit, request, routing_source=routing_source)

    async def dispatch(
        self, target, source, recorded, *, key, event_ids, instruction, accepted_state
    ):
        """Deliver a recorded native route through the same Session claim as submit."""
        no_effect = {"state": "not_applied", "executor": "pi"}
        if target is None or target != self.routing_target(source):
            return {**no_effect, "reason": "external_agent_attachment_changed"}
        try:
            content_scope = combine_scopes(
                source.payload.get("content_scope", "local_only"),
                await source_scope(self.authority.journal, event_ids[0]),
            )
            command = ExternalAgentSubmit(
                contract_version=1,
                request_id=secrets.token_hex(16),
                operation="submit",
                target=target,
                source=ExternalAgentSource(
                    event_ids=tuple(event_ids), revision=source.payload.get("revision", 1)
                ),
                idempotency_key=key,
                instruction=instruction,
                content_scope=content_scope,
                execution_policy="backend_owned",
                queue_if_busy=True,
            )
        except (httpx.HTTPError, OSError, ValueError):
            return {**no_effect, "reason": "external_agent_source_unavailable"}
        reply = await self.submit(
            ExternalAgentClaim(
                contract_version=1,
                request_id=secrets.token_hex(16),
                operation="claim",
                decision_event_id=recorded.event_id,
                command=command,
            ),
            routing_source=source,
        )
        if isinstance(reply, ExternalAgentCommandReceipt):
            state, reason = {
                "queued": ("queued", "external_agent_queued"),
                "accepted": (accepted_state, "external_agent_accepted"),
                "uncertain": ("unknown", "external_agent_unconfirmed"),
            }[reply.disposition]
            return {
                "state": state,
                "reason": reason,
                "executor": "pi",
                "external_task_id": reply.task.task_id,
                "external_task_state": reply.task.state,
            }
        return {
            "state": "unknown" if reply.reason == "unavailable" else "not_applied",
            "reason": f"external_agent_{reply.reason}",
            "executor": "pi",
        }

    async def _submit(self, request, *, routing_source=None):
        async with self._lock:
            peer = self._peer
            if self._target != request.command.target:
                return self._refuse(request.request_id, "scope_changed")
            if routing_source is not None and self.routing_target(routing_source) != self._target:
                return self._refuse(request.request_id, "scope_changed")
            if (
                self._closed
                or not self._connected
                or peer is None
                or not peer.available
                or self._observations is None
            ):
                return self._refuse(request.request_id, "unavailable")
            try:
                queued = await asyncio.to_thread(self._observations.queued_task, request)
                if queued is not None:
                    await self._bound_grant(request.command.target)
                    return self._queue_receipt(request, queued)
                waiting = await asyncio.to_thread(self._observations.next_queued)
                observed = await peer.status(secrets.token_hex(16))
                submission = PiSubmission(self.authority, peer, observations=self._observations)
                result = await submission.submit(
                    request, retain_only=observed.busy or waiting is not None
                )
                if (
                    not request.command.queue_if_busy
                    or not isinstance(result, ExternalAgentRefusal)
                    or result.reason != "busy"
                ):
                    return result
                eligibility = await self.authority.eligible_claim(request)
                if eligibility.disposition == "retained":
                    return await submission.submit(request, retain_only=True)
                queued = await asyncio.to_thread(self._observations.enqueue, request, eligibility)
                if queued is None:
                    return result
                self._queue_wake.set()
                return self._queue_receipt(request, queued)
            except (ExternalAgentNativeConflict, PiQueueConflict):
                return self._refuse(request.request_id, "scope_changed")
            except (
                ExternalAgentNativeUnavailable,
                PiCommandUnconfirmed,
                PiObservationUnavailable,
                PiPeerUnavailable,
                PermissionError,
            ):
                return self._refuse(request.request_id, "unavailable")

    @staticmethod
    def _queue_receipt(request, task):
        return ExternalAgentCommandReceipt(
            contract_version=1,
            request_id=request.request_id,
            kind="command",
            target=request.command.target,
            operation="submit",
            idempotency_key=request.command.idempotency_key,
            task=task,
            disposition="queued",
        )

    async def _run_queue(self):
        while not self._closed:
            self._queue_wake.clear()
            delay = await self._drain_queue()
            if delay is None:
                await self._queue_wake.wait()
            elif delay:
                try:
                    await asyncio.wait_for(self._queue_wake.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def _drain_queue(self):
        async with self._lock:
            peer, observations = self._peer, self._observations
            if self._closed or not self._connected or peer is None or not peer.available:
                return None
            try:
                request = await asyncio.to_thread(observations.next_queued)
                if request is None:
                    return None
                occupancy = await peer.status(secrets.token_hex(16))
                if occupancy.busy:
                    # A tracked run's terminal observation wakes the queue.
                    # Poll only while Pi reports unbound backend housekeeping.
                    return None if occupancy.run_id is not None else 0.25
                if self._closed:
                    return None
                try:
                    await PiSubmission(self.authority, peer, observations=observations).submit(
                        request
                    )
                except ExternalAgentNativeConflict:
                    await asyncio.to_thread(observations.retire_queued, request, "not_applied")
                else:
                    # A new native claim moves the pending row to starting.
                    # A retained claim can exist after a lost admission ACK.
                    await asyncio.to_thread(observations.retire_queued, request, "uncertain")
                return 0
            except (
                ExternalAgentNativeUnavailable,
                PiCommandUnconfirmed,
                PiPeerUnavailable,
                PermissionError,
                ValueError,
            ):
                return 1

    async def events(self, request):
        async with self._lock:
            try:
                await self._bound_grant(request.target)
                observations = PiObservations(self.data_dir, request.target)
                return await asyncio.to_thread(observations.events, request)
            except ExternalAgentNativeConflict:
                return self._refuse(request.request_id, "scope_changed")
            except (ExternalAgentNativeUnavailable, PermissionError, PiObservationUnavailable):
                return self._refuse(request.request_id, "unavailable")
            except PiEventReplayExpired:
                return self._refuse(request.request_id, "replay_expired")
            except ValueError:
                return self._refuse(request.request_id, "unavailable")

    async def _reap(self):
        self._connected = False
        peer, observations = self._peer, self._observations

        async def finalize():
            if peer is not None:
                try:
                    await asyncio.wait_for(peer.wait_closed(), 2)
                except TimeoutError:
                    pass
            if observations is not None:
                try:
                    await asyncio.to_thread(observations.stop)
                except PiObservationUnavailable:
                    # The process exit is still observed. A replacement must
                    # reconcile this generation's incomplete observation tail.
                    pass

        try:
            stopped = await self._owned.close(finalize)
        except (OSError, TimeoutError):
            return False
        if stopped:
            self._peer = None
        return stopped

    async def disconnect(self, request):
        return await self._perform(self._disconnect, request)

    async def _disconnect(self, request):
        async with self._lock:
            if self._target != request.target:
                return self._refuse(request.request_id, "scope_changed")
            self._connected = False
            if request.mode == "detach":
                disposition = "detached"
            else:
                disposition = "stopped" if await self._reap() else "uncertain"
            return ExternalAgentDisconnectReceipt(
                contract_version=1,
                request_id=request.request_id,
                kind="disconnect",
                target=request.target,
                disposition=disposition,
            )

    async def close(self):
        self._closed = True
        self._queue_wake.set()
        if self._queue_task is not None:
            await asyncio.shield(self._queue_task)
        if self._operations:
            await asyncio.gather(*tuple(self._operations), return_exceptions=True)
        async with self._lock:
            return self._owned is None or await self._reap()
