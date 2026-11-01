"""Codex-authored bounded HTTP adapter for standing definitions and execution."""

import asyncio
import re
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import TypeAdapter

from packages.contracts.agent_control import ControlInstance
from packages.contracts.standing_admission import StandingInitialProof, StandingTaskAdmission
from packages.contracts.standing_closure import StandingAdmissionClosure
from packages.contracts.standing_dispatch import (
    MAX_STANDING_DISPATCH_REQUEST_BYTES,
    StandingDispatchActivation,
    StandingDispatchActivationLookup,
    StandingDispatchActivationReceipt,
    StandingDispatchState,
    StandingDispatchStop,
    StandingDispatchStopProof,
    StandingProducerReceipt,
    StandingProducerRenewal,
)
from packages.contracts.standing_execution import (
    MAX_STANDING_EXECUTION_INTENT_BYTES,
    StandingExecutionClaim,
    StandingExecutionClaimAdmission,
    StandingExecutionIntent,
)
from packages.contracts.standing_export import StandingExportAdmission, StandingExportRequest
from packages.contracts.standing_resources import (
    StandingResourceObservation,
    StandingResourceReservation,
    StandingResourceReservationReceipt,
    TaskResourceContext,
    TaskResourceQuery,
)
from packages.contracts.standing_work import (
    MAX_STANDING_PROOF_BYTES,
    MAX_STANDING_REQUEST_BYTES,
    StandingChange,
    StandingChangeLookup,
    StandingChangeReceipt,
    StandingConflict,
    StandingResponsibilityPage,
    StandingResponsibilitySnapshot,
    StandingUnavailable,
    canonical_bytes,
)

MAX_STANDING_OPERATIONS = 64
STANDING_OPERATION_SECONDS = 25.0
STANDING_INGRESS_SECONDS = 5.0
_INVALID = "Invalid standing work request"
_MEDIA = "Standing work requires identity JSON"
_DISPATCH_BODIES = {
    "dispatch_activate": StandingDispatchActivation,
    "dispatch_lookup": StandingDispatchActivation,
    "dispatch_stop": StandingDispatchStop,
    "producer_renew": StandingProducerRenewal,
}
_RESOURCE_BODIES = {
    "resource_context": TaskResourceQuery,
    "resource_reserve": StandingResourceReservation,
}
_EXECUTION_BODIES = {
    "execution_prepare",
    "execution_lookup",
    "task_admit",
    "task_lookup",
    "task_close_admission",
    "task_closure_lookup",
    "resource_observe",
}
_NULLABLE_EXECUTION = {
    "execution_lookup",
    "execution_unresolved",
    "task_lookup",
    "task_closure_lookup",
}
_CAPABILITY_ACTIONS = {"task_admit", "task_close_admission"}


class StandingOwnerOperations:
    """Own actual ingress/thread/serialization work beyond facade deadlines.

    No close/timeout path cancels a thread facade and calls the thread stopped.
    Close can therefore wait for an uncooperative native callable to settle.
    """

    def __init__(self, owner, *, dispatch=None, execution=None, tasks=None, resources=None):
        self.owner = owner
        self.dispatch = dispatch
        self.execution = execution
        self.tasks = tasks
        self.resources = resources
        self._closed = False
        self._operations = {}

    @property
    def active_operations(self) -> int:
        return len(self._operations)

    def _settled(self, task):
        self._operations.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        self._closed = True
        while self._operations:
            tasks = tuple(self._operations)
            for event in self._operations.values():
                event.set()
            # Shield every real owner. Cancellation of close does not release
            # capacity or stop native work; a later close may still await it.
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    @staticmethod
    def _guard(event, deadline):
        if event.is_set() or time.monotonic() >= deadline:
            event.set()
            raise StandingUnavailable

    async def _body(self, request, event, deadline, started, *, kind=StandingChange):
        if request.headers.get("content-type", "").split(";", 1)[
            0
        ].strip().lower() != "application/json" or request.headers.get(
            "content-encoding", "identity"
        ).strip().lower() not in {"", "identity"}:
            raise HTTPException(415, _MEDIA)
        raw = bytearray()
        maximum = (
            MAX_STANDING_REQUEST_BYTES
            if kind is StandingChange
            else MAX_STANDING_DISPATCH_REQUEST_BYTES
        )
        if kind is StandingExecutionIntent:
            maximum = MAX_STANDING_EXECUTION_INTENT_BYTES
        elif kind is StandingExportRequest:
            maximum = StandingExportRequest.wire_bytes
        elif kind in (TaskResourceQuery, StandingResourceReservation):
            maximum = kind.wire_bytes
        ingress_deadline = min(deadline, started + STANDING_INGRESS_SECONDS)
        remaining = ingress_deadline - time.monotonic()
        if remaining <= 0:
            event.set()
            raise StandingUnavailable
        try:
            async with asyncio.timeout(remaining):
                async for chunk in request.stream():
                    self._guard(event, ingress_deadline)
                    if len(raw) + len(chunk) > maximum:
                        raise HTTPException(422, _INVALID)
                    raw.extend(chunk)
            self._guard(event, ingress_deadline)
        except TimeoutError:
            event.set()
            raise StandingUnavailable from None
        try:
            return kind.from_json(bytes(raw))
        except ValueError:
            raise HTTPException(422, _INVALID) from None

    @staticmethod
    def _query(request, action):
        allowed = {"after", "limit"} if action == "page" else set()
        if any(
            key not in allowed or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise HTTPException(422, _INVALID)
        if action != "page":
            return {}
        after = request.query_params.get("after")
        if after is not None:
            try:
                after = TypeAdapter(ControlInstance).validate_python(after, strict=True)
            except ValueError:
                raise HTTPException(422, _INVALID) from None
        raw_limit = request.query_params.get("limit", "20")
        if re.fullmatch(r"[1-9][0-9]?", raw_limit) is None or not 1 <= int(raw_limit) <= 20:
            raise HTTPException(422, _INVALID)
        return {"after": after, "limit": int(raw_limit)}

    @staticmethod
    def _capability(request):
        values = [
            value
            for key, value in request.scope["headers"]
            if key.lower() == b"x-kinaiya-standing-capability"
        ]
        if not values:
            return None
        if len(values) != 1 or re.fullmatch(rb"[0-9a-f]{64}", values[0]) is None:
            raise HTTPException(422, _INVALID)
        return bytes.fromhex(values[0].decode("ascii"))

    def _call(self, action, argument, query, event, deadline, *, capability=None):
        self._guard(event, deadline)
        try:
            if action == "change":
                result = self.owner.change(argument, deadline=deadline, cancelled=event.is_set)
                kind = StandingChangeReceipt
            elif action == "lookup":
                result = self.owner.lookup(argument)
                kind = StandingChangeLookup
            elif action == "snapshot":
                result = self.owner.snapshot(argument)
                kind = StandingResponsibilitySnapshot
            elif action == "page":
                result = self.owner.page(**query)
                kind = StandingResponsibilityPage
            elif action == "execution_prepare" and self.execution is not None:
                result = self.execution.prepare(argument, deadline=deadline, cancelled=event.is_set)
                kind = StandingExecutionClaimAdmission
            elif action == "execution_lookup" and self.execution is not None:
                result = self.execution.lookup(argument)
                kind = StandingExecutionClaim
            elif action == "execution_unresolved" and self.execution is not None:
                result = self.execution.unresolved()
                kind = StandingExecutionClaim
            elif action == "task_admit" and self.tasks is not None:
                result = self.tasks.admit(
                    argument, capability=capability, deadline=deadline, cancelled=event.is_set
                )
                kind = StandingTaskAdmission
            elif action == "task_export" and self.tasks is not None:
                result = self.tasks.export_admission(
                    argument, deadline=deadline, cancelled=event.is_set
                )
                kind = StandingExportAdmission
            elif action == "task_lookup" and self.tasks is not None:
                result = self.tasks.lookup(argument)
                kind = StandingInitialProof
            elif action == "task_close_admission" and self.tasks is not None:
                result = self.tasks.close_admission(
                    argument, capability=capability, deadline=deadline, cancelled=event.is_set
                )
                kind = StandingAdmissionClosure
            elif action == "task_closure_lookup" and self.tasks is not None:
                result = self.tasks.lookup_closure(argument)
                kind = StandingAdmissionClosure
            elif action == "resource_context" and self.resources is not None:
                result = self.resources.context(argument)
                kind = TaskResourceContext
            elif action == "resource_reserve" and self.resources is not None:
                result = self.resources.reserve(argument, deadline=deadline, cancelled=event.is_set)
                kind = StandingResourceReservationReceipt
            elif action == "resource_observe" and self.resources is not None:
                result = self.resources.observe(argument)
                kind = StandingResourceObservation
            elif self.dispatch is None:
                raise StandingUnavailable
            elif action == "dispatch_snapshot":
                result = self.dispatch.snapshot()
                kind = StandingDispatchState
            elif action == "dispatch_stop_context":
                result = self.dispatch.stop_context()
                kind = StandingDispatchStop
            elif action == "dispatch_activate":
                result = self.dispatch.activate(argument, deadline=deadline, cancelled=event.is_set)
                kind = StandingDispatchActivationReceipt
            elif action == "dispatch_lookup":
                result = self.dispatch.lookup(argument)
                kind = StandingDispatchActivationLookup
            elif action == "dispatch_stop":
                result = self.dispatch.stop(argument, deadline=deadline, cancelled=event.is_set)
                kind = StandingDispatchStopProof
            elif action == "producer_renew":
                result = self.dispatch.renew(argument, deadline=deadline, cancelled=event.is_set)
                kind = StandingProducerReceipt
            else:
                raise StandingUnavailable
            self._guard(event, deadline)
            if result is None and action in _NULLABLE_EXECUTION:
                return b"null"
            if not isinstance(result, kind):
                raise StandingUnavailable
            if (
                kind
                in (
                    StandingAdmissionClosure,
                    TaskResourceContext,
                    StandingResourceReservationReceipt,
                    StandingResourceObservation,
                )
                and type(result) is not kind
            ):
                raise StandingUnavailable
            value = kind.model_validate(result)
            if (
                action in _DISPATCH_BODIES
                or action in _EXECUTION_BODIES
                or action in _RESOURCE_BODIES
                or action == "task_export"
            ):
                value = value.validate_request(argument)
            raw = canonical_bytes(value)
            if len(raw) > min(kind.wire_bytes, MAX_STANDING_PROOF_BYTES):
                raise StandingUnavailable
            self._guard(event, deadline)
            return raw
        except (StandingConflict, StandingUnavailable):
            raise
        except Exception:
            raise StandingUnavailable from None

    @staticmethod
    async def _thread_result(task, event):
        interrupted = False
        while True:
            try:
                result = await asyncio.shield(task)
                if interrupted:
                    raise asyncio.CancelledError
                return result
            except asyncio.CancelledError:
                event.set()
                if task.done():
                    raise
                interrupted = True

    async def _execute(
        self, request, action, argument, event, deadline, started, ingress_done, disconnected
    ):
        monitor = None
        try:
            query = self._query(request, action)
            if (action in _RESOURCE_BODIES or action == "resource_observe") and any(
                key.lower() == b"x-kinaiya-standing-capability"
                for key, _ in request.scope["headers"]
            ):
                raise HTTPException(422, _INVALID)
            capability = self._capability(request) if action in _CAPABILITY_ACTIONS else None
            if action in {"change", "lookup"}:
                argument = await self._body(request, event, deadline, started)
            elif action in _DISPATCH_BODIES:
                argument = await self._body(
                    request, event, deadline, started, kind=_DISPATCH_BODIES[action]
                )
            elif action in _RESOURCE_BODIES:
                argument = await self._body(
                    request, event, deadline, started, kind=_RESOURCE_BODIES[action]
                )
                self._guard(event, min(deadline, started + STANDING_INGRESS_SECONDS))
            elif action in _EXECUTION_BODIES:
                argument = await self._body(
                    request, event, deadline, started, kind=StandingExecutionIntent
                )
                self._guard(event, min(deadline, started + STANDING_INGRESS_SECONDS))
            elif action == "task_export":
                argument = await self._body(
                    request, event, deadline, started, kind=StandingExportRequest
                )
                self._guard(event, min(deadline, started + STANDING_INGRESS_SECONDS))
            elif action == "snapshot":
                try:
                    argument = TypeAdapter(ControlInstance).validate_python(argument, strict=True)
                except ValueError:
                    raise HTTPException(422, _INVALID) from None
            ingress_done.set()
            self._guard(event, deadline)
            if action in _CAPABILITY_ACTIONS:
                native = asyncio.to_thread(
                    self._call, action, argument, query, event, deadline, capability=capability
                )
            else:
                native = asyncio.to_thread(self._call, action, argument, query, event, deadline)
            task = asyncio.create_task(native)
            monitor = asyncio.create_task(self._disconnect(request, event, disconnected))
            return await self._thread_result(task, event)
        except (HTTPException, StandingConflict, StandingUnavailable):
            raise
        except BaseException as error:
            event.set()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise StandingUnavailable from None
        finally:
            ingress_done.set()
            if monitor is not None:
                monitor.cancel()
                # This cleanup belongs to the retained actual operation, not
                # the bounded public facade. A resistant receive keeps its slot.
                await asyncio.gather(monitor, return_exceptions=True)

    @staticmethod
    async def _disconnect(request, event, disconnected):
        try:
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    event.set()
                    disconnected.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            event.set()
            disconnected.set()

    async def run(self, request, action, argument=None):
        if self._closed or len(self._operations) >= MAX_STANDING_OPERATIONS:
            raise StandingUnavailable
        started = time.monotonic()
        deadline = started + STANDING_OPERATION_SECONDS
        event = threading.Event()
        ingress_done = asyncio.Event()
        disconnected = asyncio.Event()
        task = asyncio.create_task(
            self._execute(
                request, action, argument, event, deadline, started, ingress_done, disconnected
            )
        )
        self._operations[task] = event
        task.add_done_callback(self._settled)
        ingress = asyncio.create_task(ingress_done.wait())
        disconnect_waiter = None
        try:
            done, _ = await asyncio.wait(
                (task, ingress),
                timeout=max(
                    0, min(deadline, started + STANDING_INGRESS_SECONDS) - time.monotonic()
                ),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                event.set()
                raise StandingUnavailable
            if task.done():
                self._guard(event, deadline)
                return task.result()
            disconnect_waiter = asyncio.create_task(disconnected.wait())
            done, _ = await asyncio.wait(
                (task, disconnect_waiter),
                timeout=max(0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task not in done or disconnect_waiter in done:
                event.set()
                raise StandingUnavailable
            self._guard(event, deadline)
            return task.result()
        except asyncio.CancelledError:
            event.set()
            raise
        finally:
            for waiter in (ingress, disconnect_waiter):
                if waiter is not None:
                    waiter.cancel()
            await asyncio.gather(
                *(waiter for waiter in (ingress, disconnect_waiter) if waiter is not None),
                return_exceptions=True,
            )


def install_standing_owner_routes(
    app, owner, auth, *, dispatch=None, execution=None, tasks=None, resources=None
) -> StandingOwnerOperations:
    """Install standing controls and Agent-only execution in one actual-owner hub."""
    operations = StandingOwnerOperations(
        owner, dispatch=dispatch, execution=execution, tasks=tasks, resources=resources
    )
    routes = APIRouter(prefix="/v1/standing", dependencies=[Depends(auth.require("owner"))])
    agent_routes = APIRouter(prefix="/v1/standing", dependencies=[Depends(auth.require("agent"))])
    resource_routes = APIRouter(
        prefix="/v1/standing/resources", dependencies=[Depends(auth.require("resources"))]
    )

    @app.middleware("http")
    async def standing_no_store(request, call_next):
        protected = request.url.path == "/v1/standing" or request.url.path.startswith(
            "/v1/standing/"
        )
        try:
            response = await call_next(request)
        except Exception:
            if not protected:
                raise
            response = JSONResponse(status_code=503, content={"detail": str(StandingUnavailable())})
        if protected:
            response.headers["Cache-Control"] = "no-store"
        return response

    async def respond(request, action, argument=None):
        try:
            raw = await operations.run(request, action, argument)
        except StandingConflict:
            raise HTTPException(409, str(StandingConflict())) from None
        except StandingUnavailable:
            raise HTTPException(503, str(StandingUnavailable())) from None
        return Response(raw, media_type="application/json", headers={"Cache-Control": "no-store"})

    @routes.post("/changes")
    async def change(request: Request):
        return await respond(request, "change")

    @routes.post("/changes/lookup")
    async def lookup(request: Request):
        return await respond(request, "lookup")

    @routes.get("/responsibilities")
    async def page(request: Request):
        return await respond(request, "page")

    @routes.get("/responsibilities/{responsibility_id}")
    async def snapshot(request: Request, responsibility_id: str):
        return await respond(request, "snapshot", responsibility_id)

    @routes.get("/dispatch")
    async def dispatch_snapshot(request: Request):
        return await respond(request, "dispatch_snapshot")

    @routes.get("/dispatch/stop-context")
    async def dispatch_stop_context(request: Request):
        return await respond(request, "dispatch_stop_context")

    @routes.post("/dispatch/activate")
    async def dispatch_activate(request: Request):
        return await respond(request, "dispatch_activate")

    @routes.post("/dispatch/activate/lookup")
    async def dispatch_lookup(request: Request):
        return await respond(request, "dispatch_lookup")

    @routes.post("/dispatch/stop")
    async def dispatch_stop(request: Request):
        return await respond(request, "dispatch_stop")

    @agent_routes.post("/producer/renew")
    async def producer_renew(request: Request):
        return await respond(request, "producer_renew")

    @agent_routes.post("/executions/prepare")
    async def execution_prepare(request: Request):
        return await respond(request, "execution_prepare")

    @agent_routes.post("/executions/lookup")
    async def execution_lookup(request: Request):
        return await respond(request, "execution_lookup")

    @agent_routes.get("/executions/unresolved")
    async def execution_unresolved(request: Request):
        return await respond(request, "execution_unresolved")

    @agent_routes.post("/tasks/admit")
    async def task_admit(request: Request):
        return await respond(request, "task_admit")

    @agent_routes.post("/tasks/lookup")
    async def task_lookup(request: Request):
        return await respond(request, "task_lookup")

    @agent_routes.post("/tasks/export-admission")
    async def task_export(request: Request):
        return await respond(request, "task_export")

    @agent_routes.post("/tasks/close-admission")
    async def task_close_admission(request: Request):
        return await respond(request, "task_close_admission")

    @agent_routes.post("/tasks/closure-lookup")
    async def task_closure_lookup(request: Request):
        return await respond(request, "task_closure_lookup")

    @resource_routes.post("/context")
    async def resource_context(request: Request):
        return await respond(request, "resource_context")

    @resource_routes.post("/reserve")
    async def resource_reserve(request: Request):
        return await respond(request, "resource_reserve")

    @agent_routes.post("/resources/observe")
    async def resource_observe(request: Request):
        return await respond(request, "resource_observe")

    app.include_router(routes)
    app.include_router(agent_routes)
    app.include_router(resource_routes)
    return operations
