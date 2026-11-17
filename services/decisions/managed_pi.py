"""Owner-selected Pi attachment and lifetime for Decisions."""

import asyncio
import secrets

import httpx

from packages.agent_adapters.external_submission import PiSubmission
from packages.agent_adapters.pi_sdk import (
    MAX_FRAME_BYTES,
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
    ExternalAgentEventPage,
    ExternalAgentGrantLookup,
    ExternalAgentMessageEvent,
    ExternalAgentOutputUnavailable,
    ExternalAgentRefusal,
    ExternalAgentSource,
    ExternalAgentStateEvent,
    ExternalAgentStatus,
    ExternalAgentSubmit,
    ExternalAgentTarget,
    ExternalAgentToolEvent,
)
from packages.contracts.external_agent_client import (
    ExternalAgentNativeConflict,
    ExternalAgentNativeUnavailable,
    ExternalAgentSessionClient,
)
from packages.providers.disclosure import combine_scopes, source_scope
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
        self._tasks = {}

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

    def _observed(self, request_id):
        target = self._target
        if target is None:
            return self._refuse(request_id, "unavailable")
        alive = self._owned is not None and self._owned.process.returncode is None
        ready = alive and self._peer is not None and self._peer.available
        state = "stopped"
        if ready:
            state = "connected" if self._connected else "detached"
        elif alive:
            state = "unavailable"
        self._refresh_tasks(ready)
        return ExternalAgentStatus(
            contract_version=1,
            request_id=request_id,
            kind="status",
            target=target,
            scope=self._grant.original.scope,
            backend="pi",
            state=state,
            tasks=tuple(self._tasks.values()),
            capabilities=ExternalAgentCapabilities(
                submit=ready and self._connected,
                queue=False,
                steer=False,
                interrupt=False,
                event_replay=ready,
                transcript_read=ready,
                approvals=False,
                artifacts=False,
                shutdown_owned=alive,
            ),
        )

    def _refresh_tasks(self, ready):
        if ready:
            for item in self._peer.retained_events():
                task = self._tasks.get(item.run_id)
                if task is not None and item.kind in {
                    "running",
                    "settled",
                    "failed",
                    "interrupted",
                    "uncertain",
                }:
                    self._tasks[item.run_id] = task.model_copy(update={"state": item.kind})
        else:
            for key, task in self._tasks.items():
                if task.state in {"starting", "running", "interrupt_requested"}:
                    self._tasks[key] = task.model_copy(update={"state": "uncertain"})

    async def _lookup(self, attachment_id):
        return await self.authority.lookup_grant(
            ExternalAgentGrantLookup(
                contract_version=1,
                request_id=secrets.token_hex(16),
                operation="lookup_grant",
                attachment_id=attachment_id,
            )
        )

    async def status(self, request):
        async with self._lock:
            if self._target != request.target:
                return self._refuse(request.request_id, "scope_changed")
            try:
                current = await self._lookup(request.target.attachment_id)
            except ExternalAgentNativeConflict:
                return self._refuse(request.request_id, "scope_changed")
            except (ExternalAgentNativeUnavailable, PermissionError):
                return self._refuse(request.request_id, "unavailable")
            if current.backend_session_id != request.target.backend_session_id:
                return self._refuse(request.request_id, "scope_changed")
            return self._observed(request.request_id)

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
                return self._observed(request.request_id)
            if self._owned is not None:
                await self._owned.close()
            self._owned = self._peer = self._grant = None
            self._connected = False
            self._tasks.clear()
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
            return self._observed(request.request_id)

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
                queue_if_busy=False,
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
            accepted = reply.disposition == "accepted"
            return {
                "state": accepted_state if accepted else "unknown",
                "reason": "external_agent_accepted" if accepted else "external_agent_unconfirmed",
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
            if self._closed or not self._connected or peer is None or not peer.available:
                return self._refuse(request.request_id, "unavailable")
            try:
                observed = await peer.status(secrets.token_hex(16))
                result = await PiSubmission(self.authority, peer).submit(
                    request, retain_only=observed.busy
                )
            except ExternalAgentNativeConflict:
                return self._refuse(request.request_id, "scope_changed")
            except (
                ExternalAgentNativeUnavailable,
                PiCommandUnconfirmed,
                PiPeerUnavailable,
                PermissionError,
            ):
                return self._refuse(request.request_id, "unavailable")
            if isinstance(result, ExternalAgentCommandReceipt):
                self._tasks.setdefault(result.task.task_id, result.task)
                if len(self._tasks) > 128:
                    self._tasks.pop(next(iter(self._tasks)))
            return result

    async def events(self, request):
        async with self._lock:
            peer = self._peer
            if self._target != request.target:
                return self._refuse(request.request_id, "scope_changed")
            if peer is None or not peer.available:
                return self._refuse(request.request_id, "unavailable")
            try:
                current = await self._lookup(request.target.attachment_id)
                if current.backend_session_id != request.target.backend_session_id:
                    return self._refuse(request.request_id, "scope_changed")
                records = peer.events(request.after, request.limit)
            except ExternalAgentNativeConflict:
                return self._refuse(request.request_id, "scope_changed")
            except (ExternalAgentNativeUnavailable, PermissionError):
                return self._refuse(request.request_id, "unavailable")
            except (PiEventReplayExpired, ValueError):
                return self._refuse(request.request_id, "replay_expired")
            events, size = [], 0
            for record in records:
                identity = {"cursor": record.cursor, "task_id": record.run_id}
                if record.kind == "assistant_message":
                    event = ExternalAgentMessageEvent(
                        kind=record.kind, text=record.text, **identity
                    )
                elif record.kind == "tool_activity":
                    event = ExternalAgentToolEvent(
                        kind=record.kind,
                        tool_call_id=record.tool_call_id,
                        tool_name=record.tool_name,
                        phase=record.phase,
                        **identity,
                    )
                elif record.kind == "output_unavailable":
                    event = ExternalAgentOutputUnavailable(kind=record.kind, **identity)
                else:
                    event = ExternalAgentStateEvent(
                        kind="task_state",
                        state=record.kind,
                        revision=1,
                        **identity,
                    )
                size += len(event.model_dump_json().encode("utf-8")) + 1
                # The SDK can publish a full-size single event. Bound the
                # whole response and let the cursor carry the remaining events.
                if size > MAX_FRAME_BYTES + 4096:
                    break
                events.append(event)
            if records and not events:
                return self._refuse(request.request_id, "unavailable")
            cursor = events[-1].cursor if events else request.after
            return ExternalAgentEventPage(
                contract_version=1,
                request_id=request.request_id,
                kind="events",
                target=request.target,
                after=request.after,
                next_after=cursor,
                has_more=cursor < peer.event_cursor,
                events=tuple(events),
            )

    async def _reap(self):
        self._connected = False
        self._refresh_tasks(self._peer is not None and self._peer.available)
        try:
            stopped = await self._owned.close()
        except (OSError, TimeoutError):
            return False
        if stopped:
            self._refresh_tasks(self._peer is not None and self._peer.available)
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
        if self._operations:
            await asyncio.gather(*tuple(self._operations), return_exceptions=True)
        async with self._lock:
            return self._owned is None or await self._reap()
