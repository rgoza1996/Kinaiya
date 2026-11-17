"""Owner-selected Pi attachment and lifetime for Decisions."""

import asyncio
import secrets

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
    ExternalAgentCommandReceipt,
    ExternalAgentDisconnectReceipt,
    ExternalAgentEventPage,
    ExternalAgentGrantLookup,
    ExternalAgentMessageEvent,
    ExternalAgentOutputUnavailable,
    ExternalAgentRefusal,
    ExternalAgentStateEvent,
    ExternalAgentStatus,
    ExternalAgentTarget,
    ExternalAgentToolEvent,
)
from packages.contracts.external_agent_client import (
    ExternalAgentNativeConflict,
    ExternalAgentNativeUnavailable,
    ExternalAgentSessionClient,
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

    async def _perform(self, action, request):
        if self._closed or len(self._operations) >= 8:
            return self._refuse(request.request_id, "unavailable")
        task = asyncio.create_task(action(request))
        self._operations.add(task)

        def finished(done):
            self._operations.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def attach(self, request):
        return await self._perform(self._attach, request)

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
                self._connected = True
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
            except (
                ExternalAgentNativeConflict,
                ExternalAgentNativeUnavailable,
                OSError,
                PiPeerUnavailable,
                PiLaunchUnavailable,
            ):
                if self._owned is not None:
                    await self._reap()
                return self._refuse(request.request_id, "unavailable")
            self._connected = True
            return self._observed(request.request_id)

    async def _read_transcript(self, **position):
        grant = self._grant
        if grant is None:
            raise ExternalAgentNativeUnavailable
        return await self.authority.read_bound_transcript(grant, **position)

    async def submit(self, request):
        return await self._perform(self._submit, request)

    async def _submit(self, request):
        async with self._lock:
            peer = self._peer
            if self._target != request.command.target:
                return self._refuse(request.request_id, "scope_changed")
            if self._closed or not self._connected or peer is None or not peer.available:
                return self._refuse(request.request_id, "unavailable")
            try:
                observed = await peer.status(secrets.token_hex(16))
                if observed.busy:
                    return self._refuse(request.request_id, "busy")
                result = await PiSubmission(self.authority, peer).submit(request)
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
