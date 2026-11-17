"""Agent-side ownership of optional remote Decisions advice."""

import asyncio
import hashlib
import re
from contextvars import Context
from dataclasses import dataclass, field

import httpx

from packages.contracts.agent_advice import encoded
from packages.contracts.agent_advice_operations import (
    MAX_ADVICE_LOOKUP_BYTES,
    MAX_ADVICE_OBSERVATION_BYTES,
    AgentAdviceCall,
    AgentAdviceLookup,
    AgentAdviceLookupResponse,
    AgentAdviceObservation,
    AgentAdviceRuntimeState,
    advice_json,
)
from packages.contracts.agent_control import AgentTaskIdentity
from packages.contracts.models import ContentScope
from packages.contracts.operation_ownership import ActualOperation, current_owner
from packages.providers.disclosure import combine_scopes

_BASE = "/v1/decisions/agent-advice"
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_UNCERTAIN = frozenset({"setup_unconfirmed", "cleanup_unconfirmed"})


class AdviceUnavailable(RuntimeError):
    pass


class AdviceConflict(AdviceUnavailable):
    pass


class AdviceDispatchRefused(AdviceUnavailable):
    """Trusted native authority withdrew before the original was sent."""


class _CloseUnconfirmed(AdviceUnavailable):
    pass


@dataclass(eq=False)
class _Original:
    lookup: AgentAdviceLookup
    task: AgentTaskIdentity
    revision: int
    policy_sha256: str
    scope: ContentScope
    parent: ActualOperation | None
    pending: dict[str, asyncio.Task] = field(default_factory=dict)
    terminal: AgentAdviceObservation | None = None
    cancel_observed: bool = False
    cleanup_failed: bool = False
    remote_cleanup_unconfirmed: bool = False
    active: bool = True
    dispatch_refused: bool = False

    def validate(self, observation):
        observation.validate_lookup(self.lookup)
        cleanup = (
            "confirmed"
            if observation.phase in _TERMINAL
            else "unconfirmed"
            if observation.phase in _UNCERTAIN
            else "pending"
        )
        if (
            observation.task != self.task
            or observation.objective_revision != self.revision
            or observation.policy_sha256 != self.policy_sha256
            or combine_scopes(self.scope, observation.content_scope) != observation.content_scope
            or observation.cleanup != cleanup
        ):
            raise AdviceConflict("Decisions advice observation changed its native binding")
        return observation


class DecisionsAdviceClient:
    """Retain exact remote originals independently of HTTP observer lifetimes."""

    def __init__(self, url, token, *, transport=None, capacity=8, retention=64):
        address = (
            re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})/?", url) if type(url) is str else None
        )
        if address is None or not 1 <= int(address[1]) <= 65535:
            raise ValueError("Decisions advice requires a literal loopback endpoint")
        if (
            type(token) is not str
            or len(token) < 24
            or not token.isascii()
            or any(not 33 <= ord(character) <= 126 for character in token)
        ):
            raise ValueError("Decisions advice requires its Agent service credential")
        if (
            type(capacity) is not int
            or type(retention) is not int
            or not 1 <= capacity <= retention <= 128
        ):
            raise ValueError("Invalid Decisions advice custody limits")
        self._http = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
            timeout=15,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._binding = self._selection()
        self._capacity, self._retention = capacity, retention
        self._originals: dict[str, _Original] = {}
        self._exchange_tasks: set[asyncio.Task] = set()
        self._loop = None
        self._closing = False
        self._cleanup_failed = False
        self._close_task = None

    def _selection(self):
        http = self._http
        return (http, http.base_url, tuple(http.headers.raw), http._transport)

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if self._loop is not loop:
            raise AdviceUnavailable("Decisions advice belongs to a different event loop")
        return loop

    def _check_transport(self):
        if (
            self._cleanup_failed
            or self._close_task is not None
            or self._http.is_closed
            or self._selection() != self._binding
        ):
            raise AdviceUnavailable("Original Decisions advice transport is unavailable")

    @staticmethod
    def _deadline(timeout):
        if type(timeout) not in {int, float} or not 0 < timeout <= 30:
            raise ValueError("Invalid Decisions advice observation deadline")
        return timeout

    def _original(self, lookup):
        original = self._originals.get(lookup.logical_request_id)
        if original is None or original.lookup != lookup:
            raise AdviceConflict("Decisions advice lookup does not name an enrolled original")
        return original

    async def runtime(self, *, timeout=2.5):
        self._deadline(timeout)
        if self._closing:
            raise AdviceUnavailable("Decisions advice client is retiring")
        return await self._observe(self._start("runtime"), timeout)

    async def submit(self, request, *, timeout=2.5, current=None):
        self._deadline(timeout)
        self._check_loop()
        if current is not None and not callable(current):
            raise ValueError("Decisions advice requires a callable native dispatch guard")
        call = AgentAdviceCall.model_validate(request)
        wire = call.canonical_bytes()
        lookup = AgentAdviceLookup(
            expected_instance=call.expected_instance,
            logical_request_id=call.logical_request_id,
            request_sha256=hashlib.sha256(wire).hexdigest(),
        )
        if lookup.logical_request_id in self._originals:
            # A duplicate is an observation, even after an uncertain send.
            return await self.lookup(lookup, timeout=timeout)
        if (
            self._closing
            or len(self._originals) >= self._retention
            or sum(original.active for original in self._originals.values()) >= self._capacity
        ):
            raise AdviceUnavailable("Decisions advice custody is unavailable")
        self._check_start()
        original = _Original(
            lookup,
            call.task,
            call.objective_revision,
            call.policy_sha256,
            call.content_scope,
            current_owner(),
        )
        if original.parent is not None:
            original.parent.hold(original)
        self._originals[lookup.logical_request_id] = original
        return await self._observe(
            self._start("requests", original, wire=wire, current=current), timeout
        )

    async def lookup(self, request, *, timeout=2.5):
        self._deadline(timeout)
        self._check_loop()
        original = self._original(AgentAdviceLookup.model_validate(request))
        if original.terminal is not None:
            return original.terminal
        if original.dispatch_refused:
            return None
        return await self._observe(self._start("lookup", original), timeout)

    async def cancel(self, request, *, timeout=2.5):
        self._deadline(timeout)
        self._check_loop()
        original = self._original(AgentAdviceLookup.model_validate(request))
        if original.terminal is not None:
            return original.terminal
        if original.dispatch_refused:
            return None
        route = "lookup" if original.cancel_observed else "cancel"
        return await self._observe(self._start(route, original), timeout)

    def cleanup_confirmed(self, request):
        self._check_loop()
        return not self._original(AgentAdviceLookup.model_validate(request)).active

    def custody(self):
        pending = sum(original.active for original in self._originals.values())
        failed = self._cleanup_failed or any(
            original.active and original.remote_cleanup_unconfirmed
            for original in self._originals.values()
        )
        if self._close_task is not None:
            if self._close_task.done():
                failed |= self._close_task.cancelled() or self._close_task.exception() is not None
            else:
                pending += 1
        return pending + len(self._exchange_tasks), int(failed)

    @staticmethod
    async def _observe(task, timeout):
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise AdviceUnavailable("Original Decisions advice observation timed out")
        if task.cancelled():
            raise AdviceUnavailable("Original Decisions advice exchange was interrupted")
        return task.result()

    def _check_start(self):
        loop = self._check_loop()
        self._check_transport()
        if len(self._exchange_tasks) >= 32:
            raise AdviceUnavailable("Decisions advice HTTP custody is at its bound")
        return loop

    def _start(self, route, original=None, *, wire=None, current=None):
        if original is not None and route in original.pending:
            return original.pending[route]
        loop = self._check_start()
        if original is not None and wire is None:
            wire = encoded(original.lookup.model_dump(mode="json"))
        observer_owner = current_owner()
        if observer_owner is not None and observer_owner.finished:
            raise AdviceUnavailable("Original advice caller has retired")
        # Construction gives custody of the Task before dispatch without a
        # configurable task factory or a borrowed execution/grant context.
        task = asyncio.Task(
            self._exchange(route, original, wire, current), loop=loop, context=Context()
        )
        self._exchange_tasks.add(task)
        if original is not None:
            original.pending[route] = task
        if observer_owner is not None:
            observer_owner.hold(task)
        task.add_done_callback(
            lambda done: self._settled(route, original, observer_owner, done), context=Context()
        )
        return task

    def _settled(self, route, original, observer_owner, done):
        self._exchange_tasks.discard(done)
        error = None if done.cancelled() else done.exception()
        cleanup_failed = done.cancelled() or isinstance(error, _CloseUnconfirmed)
        if cleanup_failed:
            self._cleanup_failed = True
        if original is not None:
            original.pending.pop(route)
            original.cleanup_failed |= cleanup_failed
            if isinstance(error, AdviceDispatchRefused):
                original.dispatch_refused = True
            if not cleanup_failed and error is None:
                observation = done.result()
                if observation is not None:
                    original.cancel_observed |= observation.cancellation_requested
                    if observation.phase in _TERMINAL and original.terminal is None:
                        original.terminal = observation
                    elif observation.phase in _UNCERTAIN:
                        original.remote_cleanup_unconfirmed = True
                        if original.parent is not None:
                            original.parent.fail()
            if (
                original.active
                and (original.terminal is not None or original.dispatch_refused)
                and not original.pending
                and not original.cleanup_failed
            ):
                original.active = False
                if original.parent is not None:
                    original.parent.release(original)
                    original.parent = None
            elif cleanup_failed and original.parent is not None:
                original.parent.fail()
        if observer_owner is not None:
            observer_owner.release(done, failed=cleanup_failed)

    async def _exchange(self, route, original, wire, current):
        maximum = MAX_ADVICE_LOOKUP_BYTES if route == "runtime" else MAX_ADVICE_OBSERVATION_BYTES
        try:
            raw = await self._read(route, wire, maximum, current)
            self._check_transport()
            value = advice_json(raw, maximum=maximum)
            if route == "runtime":
                return AgentAdviceRuntimeState.model_validate(value)
            if route == "requests":
                return original.validate(AgentAdviceObservation.model_validate(value))
            result = AgentAdviceLookupResponse.model_validate(value)
            if result.request != original.lookup:
                raise AdviceConflict("Decisions advice lookup changed its original request")
            if result.observation is not None:
                observation = original.validate(result.observation)
                if route == "cancel" and not observation.cancellation_requested:
                    raise AdviceConflict("Decisions advice cancellation was not acknowledged")
                return observation
            return None
        except AdviceUnavailable:
            raise
        except Exception:
            raise AdviceUnavailable("Original Decisions advice response is unavailable") from None

    async def _read(self, route, wire, maximum, current):
        response = None
        try:
            self._check_transport()
            request = self._http.build_request(
                "GET" if wire is None else "POST",
                _BASE + "/" + route,
                headers={"Content-Type": "application/json"} if wire is not None else None,
                content=wire,
            )
            async with asyncio.timeout(15):
                if current is not None:
                    try:
                        permitted = current() is True
                    except Exception:
                        permitted = False
                    if not permitted:
                        raise AdviceDispatchRefused(
                            "Native Agent task authority changed before dispatch"
                        )
                response = await self._http.send(request, stream=True, follow_redirects=False)
                if response.status_code == 409:
                    raise AdviceConflict("Original Decisions advice identity changed")
                if response.status_code != (202 if route == "requests" else 200):
                    raise AdviceUnavailable("Original Decisions advice request was unavailable")
                length = self._response_length(response.headers, maximum)
                raw = bytearray()
                # HTTPX's decoded/raw iterators close at EOF. The actual
                # exchange owns a single explicit close outside the read deadline.
                async for chunk in response.stream:
                    if type(chunk) is not bytes or len(raw) + len(chunk) > maximum:
                        raise AdviceUnavailable(
                            "Decisions advice response exceeded its resource bound"
                        )
                    raw.extend(chunk)
                if length is not None and length != len(raw):
                    raise AdviceUnavailable("Decisions advice response length changed")
                return bytes(raw)
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except (Exception, asyncio.CancelledError):
                    raise _CloseUnconfirmed(
                        "Decisions advice response close is unconfirmed"
                    ) from None

    @staticmethod
    def _response_length(headers, maximum):
        if any(
            len(headers.get_list(name)) > 1
            for name in (
                "content-type",
                "content-encoding",
                "content-length",
                "cache-control",
            )
        ) or (
            headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json"
            or headers.get("content-encoding", "identity").strip().lower() not in {"", "identity"}
            or headers.get("cache-control", "").strip().lower() != "no-store"
        ):
            raise AdviceUnavailable("Invalid Decisions advice response envelope")
        declared = headers.get("content-length")
        if declared is None:
            return None
        if (
            "transfer-encoding" in headers
            or not declared.isascii()
            or not declared.isdigit()
            or len(declared) > 8
            or int(declared) > maximum
        ):
            raise AdviceUnavailable("Invalid Decisions advice response length")
        return int(declared)

    async def close(self, *, timeout=1.0):
        """Request exact remote cancellation and observe cleanup within the deadline."""
        deadline = self._check_loop().time() + self._deadline(timeout)
        self._closing = True
        if self._close_task is None:
            for original in self._originals.values():
                if original.active and original.terminal is None and not original.dispatch_refused:
                    try:
                        self._start("lookup" if original.cancel_observed else "cancel", original)
                    except AdviceUnavailable:
                        break
            if self._exchange_tasks:
                await asyncio.wait(
                    tuple(self._exchange_tasks), timeout=max(0, deadline - self._loop.time())
                )
            if (
                self._exchange_tasks
                or self._cleanup_failed
                or any(original.active for original in self._originals.values())
            ):
                return False
            if self._close_task is None:
                owner = current_owner()
                if owner is not None and owner.finished:
                    return False
                self._close_task = asyncio.Task(
                    self._binding[0].aclose(), loop=self._loop, context=Context()
                )
                if owner is not None:
                    owner.retain_task(self._close_task, cleanup=True)
                self._close_task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None,
                    context=Context(),
                )
        done, _ = await asyncio.wait(
            {self._close_task}, timeout=max(0, deadline - self._loop.time())
        )
        return (
            bool(done) and not self._close_task.cancelled() and self._close_task.exception() is None
        )
