"""One retained original per advice request, including its actual adapter close.

This internal owner is not installed in the public service yet. The authenticated
Agent bridge and runtime guards must be integrated before enabling requests.
"""

import asyncio
import contextvars
import hashlib
import secrets
from dataclasses import dataclass, field

from packages.contracts.agent_advice import AgentAdviceExportAdmission, AgentAdviceExportRequest
from packages.contracts.agent_advice_operations import (
    MAX_ADVICE_OUTPUT_BYTES,
    AgentAdviceCall,
    AgentAdviceLookup,
    AgentAdviceModelPin,
    AgentAdviceObservation,
)
from packages.contracts.agent_tools import advisory_text_scope
from packages.contracts.operation_ownership import ActualOperation, actual_owner, current_owner
from packages.providers.completion_budget import PreparedComplete
from packages.providers.disclosure import combine_scopes, scope_allowed
from packages.providers.text import ScriptedProvider
from services.agent.task_model_lease import TaskModelLease
from services.decisions.agent_policy import AgentPolicyUnavailable
from services.settings.provider_factory import open_captured_recipe


class AgentAdviceConflict(ValueError):
    pass


class AgentAdviceUnavailable(RuntimeError):
    pass


@dataclass(eq=False)
class _Original:
    call: AgentAdviceCall
    raw: bytes
    digest: str
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    task: object = None
    coroutine: object = None
    entered: object = None
    phase: str = "pending"
    cleanup: str = "pending"
    cancel_requested: bool = False
    setup_unknown: bool = False
    active: bool = True
    creation_attempted: bool = False
    provider: object = None
    lease: object = None
    model_pin: object = None
    content_scope: str = "local_only"
    model_returned: bool = False
    output_state: str = "not_returned"
    text: str | None = None
    output_sha256: str | None = None
    output_bytes: int | None = None
    generation_admission: bool = False
    native_refused: bool = False
    error: str | None = None
    ownership: object = None
    ownership_held: bool = False
    actual: object = None
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: str = "failed"
    local_cleanup_confirmed: bool = False


class AgentAdviceOperations:
    def __init__(self, policy, journal, *, resource_state=None, capacity=8, retention=64):
        if (
            type(capacity) is not int
            or type(retention) is not int
            or not 1 <= capacity <= retention <= 128
        ):
            raise ValueError("Invalid native advice operation limits")
        self.policy, self.journal, self.resource_state = policy, journal, resource_state
        self.capacity, self.retention = capacity, retention
        self.instance_id = secrets.token_hex(16)
        self._originals = {}
        self._loop = None
        self._started = self._closed = False
        self._changed = asyncio.Event()

    async def start(self):
        loop = asyncio.get_running_loop()
        if self._closed or (self._loop is not None and self._loop is not loop):
            raise AgentAdviceUnavailable("Original advice controller has retired")
        self._loop, self._started = loop, True

    def _check_loop(self):
        if not self._started or asyncio.get_running_loop() is not self._loop:
            raise AgentAdviceUnavailable("Original advice controller is unavailable")

    def _identity(self, lookup):
        self._check_loop()
        lookup = AgentAdviceLookup.model_validate(lookup)
        if lookup.expected_instance != self.instance_id:
            raise AgentAdviceConflict("Original advice controller identity changed")
        original = self._originals.get(lookup.logical_request_id)
        if original is not None and original.digest != lookup.request_sha256:
            raise AgentAdviceConflict("Advice key belongs to a different original request")
        return original

    def submit(self, call):
        self._check_loop()
        # Detach mutable lists/maps from every caller before claiming the key.
        call = AgentAdviceCall.model_validate(AgentAdviceCall.model_validate(call).model_dump())
        lookup = AgentAdviceLookup.of(call)
        original = self._identity(lookup)
        if original is not None:
            return self._observe(original)
        if self._closed:
            raise AgentAdviceUnavailable("Original advice controller has retired")
        if (
            len(self._originals) >= self.retention
            or sum(o.active for o in self._originals.values()) >= self.capacity
        ):
            raise AgentAdviceUnavailable("Original advice custody is at its declared bound")
        snapshot = self.policy.snapshot()
        if snapshot.source != "host" or snapshot.policy_sha256 != call.policy_sha256:
            raise AgentAdviceConflict("Independent Agent advice policy changed")
        original = _Original(
            call,
            call.canonical_bytes(),
            lookup.request_sha256,
            content_scope=combine_scopes(call.content_scope, snapshot.policy.content_scope),
            ownership=current_owner(),
        )
        original.actual = ActualOperation(lambda owner: self._settled(original, owner))
        # This sentinel survives an unknown Task-factory return. No orphan's
        # eventual exit can prove that the original registration was sound.
        original.actual.hold(original)
        self._originals[call.logical_request_id] = original
        try:
            if original.ownership is not None:
                original.ownership.hold(original)
                original.ownership_held = True
            original.coroutine = self._run(original)
            # No inherited execution-tool snapshot, grant or primary-model
            # callback crosses this independently owned service operation.
            task = asyncio.create_task(original.coroutine, context=contextvars.Context())
            original.task = task
            if (
                not isinstance(task, asyncio.Task)
                or task.get_loop() is not self._loop
                or task.get_coro() is not original.coroutine
                or (original.entered is not None and original.entered is not task)
            ):
                raise RuntimeError("Original advice task custody is unavailable")
            task.add_done_callback(lambda done: self._done(original, done))
            original.actual.retain_task(task)
            if original.ownership is not None:
                original.ownership.retain_task(task)
            original.actual.release(original)
            original.gate.set()
        except BaseException:
            original.setup_unknown = True
            original.phase, original.cleanup = "setup_unconfirmed", "unconfirmed"
            original.error = "original_setup_unconfirmed"
            original.actual.fail()
            if original.ownership is not None:
                original.ownership.fail()
            # Its gate stays closed. A lost/foreign task handle is not evidence
            # that the original did not start, and never releases its capacity.
            raise AgentAdviceUnavailable("Original advice setup is unconfirmed") from None
        return self._observe(original)

    def lookup(self, lookup):
        original = self._identity(lookup)
        return None if original is None else self._observe(original)

    async def wait(self, lookup, *, timeout=1.0):
        if type(timeout) not in {int, float} or isinstance(timeout, bool) or not 0 < timeout <= 30:
            raise ValueError("Invalid advice observation deadline")
        original = self._identity(lookup)
        if original is None:
            return None
        observer = asyncio.current_task()
        cancellations = observer.cancelling()
        if original.task is not None and not original.setup_unknown:
            try:
                async with asyncio.timeout(timeout):
                    try:
                        await asyncio.shield(original.task)
                    except asyncio.CancelledError:
                        if observer.cancelling() != cancellations or not original.task.cancelled():
                            raise
                        self._done(original, original.task)
                    # Root completion can precede a retained metadata close.
                    # Cancelling this passive Event wait owns no actual work.
                    await original.settled.wait()
            except TimeoutError:
                pass
        return self._observe(original)

    def cancel(self, lookup):
        original = self._identity(lookup)
        if original is None:
            return None
        original.cancel_requested = True
        if original.task is not None and not original.setup_unknown and not original.task.done():
            original.task.cancel()
        return self._observe(original)

    def _done(self, original, task):
        if original.setup_unknown:
            return
        if task.cancelled() and original.entered is None:
            original.outcome, original.local_cleanup_confirmed = "cancelled", True
        elif task.cancelled() or task.exception() is not None:
            original.phase, original.cleanup = "cleanup_unconfirmed", "unconfirmed"
            original.error = "actual_cleanup_unconfirmed"
            original.actual.fail()
            if original.ownership is not None:
                original.ownership.fail()

    def _settled(self, original, owner):
        if not owner.finished or original.setup_unknown:
            return
        if owner.failed or not original.local_cleanup_confirmed:
            if original.phase != "setup_unconfirmed":
                original.phase, original.cleanup = "cleanup_unconfirmed", "unconfirmed"
                original.error = "actual_cleanup_unconfirmed"
            if original.ownership is not None:
                original.ownership.fail()
        else:
            original.phase, original.cleanup, original.active = original.outcome, "confirmed", False
            if original.ownership is not None and original.ownership_held:
                original.ownership_held = False
                original.ownership.release(original)
        original.settled.set()
        self._changed.set()

    def _observe(self, original):
        return AgentAdviceObservation(
            instance_id=self.instance_id,
            logical_request_id=original.call.logical_request_id,
            request_sha256=original.digest,
            policy_sha256=original.call.policy_sha256,
            task=original.call.task,
            objective_revision=original.call.objective_revision,
            phase=original.phase,
            cancellation_requested=original.cancel_requested,
            cleanup=original.cleanup,
            model_pin=original.model_pin,
            content_scope=original.content_scope,
            model_returned=original.model_returned,
            output_state=original.output_state,
            model_output=original.text,
            output_sha256=original.output_sha256,
            output_bytes=original.output_bytes,
            generation_admission_observed=original.generation_admission,
            error=original.error,
        )

    async def _run(self, original):
        original.entered = asyncio.current_task()
        # Preserve only the trusted actual-operation owner across the fresh
        # execution/export context. It cannot be supplied in an advice DTO.
        with actual_owner(original.actual):
            await self._execute(original)

    async def _execute(self, original):
        captured = prepared = None
        outcome = "failed"
        try:
            await original.gate.wait()
            if original.setup_unknown or original.entered is not original.task:
                raise AgentAdviceUnavailable("Original advice task custody changed")
            if original.cancel_requested or self._closed:
                raise asyncio.CancelledError
            original.phase = "running"
            policy, captured = self.policy.capture(original.call.policy_sha256)
            recipe, info = captured.recipe, captured.recipe.info
            if not scope_allowed(info, original.content_scope):
                raise AgentPolicyUnavailable("Advice input disclosure was refused")
            original.creation_attempted = True
            provider = open_captured_recipe(
                captured, auxiliary=True, resource_state=self.resource_state, role="agent"
            )
            original.provider = provider
            original.lease = TaskModelLease(lambda lease: None)
            original.lease.retain(original.actual)
            original.lease.own(provider)
            if provider.info != info:
                raise AgentPolicyUnavailable("Captured advice provider identity changed")
            original.model_pin = AgentAdviceModelPin(
                policy_sha256=policy.fingerprint(),
                catalog_binding=recipe.binding,
                catalog_revision=recipe.catalog_revision,
                provider=info.model_dump(),
                reasoning=recipe.reasoning,
            )
            messages = [
                {"role": "system", "content": policy.instructions},
                *[message.model_dump() for message in original.call.messages],
            ]

            def current():
                return (
                    asyncio.current_task() is original.task
                    and not self._closed
                    and not original.cancel_requested
                    and not original.setup_unknown
                    and original.call.canonical_bytes() == original.raw
                    and original.provider is provider
                    and provider.info == info
                    and policy.fingerprint() == original.call.policy_sha256
                )

            async def admit(purpose, request_sha256):
                if not current():
                    raise AgentAdviceUnavailable("Original advice upload authority changed")
                request = AgentAdviceExportRequest(
                    task=original.call.task,
                    objective_revision=original.call.objective_revision,
                    source_revisions=dict(original.call.source_revisions),
                    context_event_ids=list(original.call.context_event_ids),
                    content_scope=original.call.content_scope,
                    instruction_scope=policy.content_scope,
                    provider=info.model_dump(),
                    policy_sha256=policy.fingerprint(),
                    logical_request_id=original.call.logical_request_id,
                    request_sha256=request_sha256,
                    purpose=purpose,
                    request_nonce=secrets.token_hex(16),
                )
                try:
                    receipt = await self.journal.agent_advice_export_admission(request)
                    if type(receipt) is not AgentAdviceExportAdmission:
                        raise ValueError("Native advice export receipt is invalid")
                    AgentAdviceExportAdmission.model_validate(
                        receipt.model_dump()
                    ).validate_request(request)
                    if not current():
                        raise AgentAdviceUnavailable("Original advice upload authority changed")
                except BaseException:
                    original.native_refused = True
                    raise
                original.content_scope = combine_scopes(
                    original.content_scope, receipt.export.effective_scope
                )
                if purpose == "complete_generation":
                    original.generation_admission = True

            with advisory_text_scope(current, admit):
                prepared = await provider.prepare_complete(messages)
                adapter = getattr(provider, "provider", provider)
                if type(prepared) is not PreparedComplete or (
                    prepared.fixture and not isinstance(adapter, ScriptedProvider)
                ):
                    raise AgentAdviceUnavailable("Measured advice preparation is unavailable")
                text = await prepared.complete()
            original.model_returned = True
            if type(text) is not str:
                original.output_state, original.error = "invalid_text", "invalid_output"
            else:
                try:
                    raw = text.encode("utf-8")
                    original.output_sha256, original.output_bytes = (
                        hashlib.sha256(raw).hexdigest(),
                        len(raw),
                    )
                    if len(raw) > MAX_ADVICE_OUTPUT_BYTES:
                        original.output_state, original.error = (
                            "resource_refused",
                            "output_exceeds_bound",
                        )
                    else:
                        original.text, original.output_state, outcome = (
                            text,
                            "retained",
                            "completed",
                        )
                except UnicodeError:
                    original.output_state, original.error = "invalid_text", "invalid_output"
        except asyncio.CancelledError:
            outcome = "cancelled"
        except AgentPolicyUnavailable:
            original.error = "policy_unavailable"
        except Exception:
            original.error = (
                "native_admission_refused" if original.native_refused else "provider_failed"
            )
        finally:
            original.outcome = outcome
            if isinstance(prepared, PreparedComplete):
                prepared.discard()
            if captured is not None:
                captured.release()
            if original.lease is not None:
                original.phase = "closing"
                close = original.lease.start_close()
                while not close.done():
                    try:
                        await asyncio.shield(close)
                    except asyncio.CancelledError:
                        original.cancel_requested = True
                    except Exception:
                        break
                try:
                    close.result()
                    if not original.lease.settled:
                        raise AgentAdviceUnavailable("Actual advice close is unconfirmed")
                except BaseException:
                    original.phase, original.cleanup = "cleanup_unconfirmed", "unconfirmed"
                    original.error = "actual_cleanup_unconfirmed"
                else:
                    original.local_cleanup_confirmed = True
            elif original.creation_attempted:
                original.phase, original.cleanup = "setup_unconfirmed", "unconfirmed"
                original.error = "original_setup_unconfirmed"
            else:
                original.local_cleanup_confirmed = True
            if original.setup_unknown:
                # A queued coroutine can exit after its factory lost the Task
                # return. Its finally block cannot validate that missing or
                # foreign registration or release the original custody bound.
                original.phase, original.cleanup = "setup_unconfirmed", "unconfirmed"
                original.error = "original_setup_unconfirmed"
            if original.setup_unknown or not original.local_cleanup_confirmed:
                original.actual.fail()
                if original.ownership is not None:
                    original.ownership.fail()
            elif original.cleanup == "pending":
                # The retained original, all native proof exchanges and sole
                # closes must settle before publishing a terminal observation.
                original.phase = "closing"

    async def close(self, *, timeout=1.0):
        self._check_loop()
        if type(timeout) not in {int, float} or isinstance(timeout, bool) or not 0 < timeout <= 30:
            raise ValueError("Invalid advice cleanup observation deadline")
        self._closed = True
        for original in self._originals.values():
            if original.active and original.task is not None and not original.setup_unknown:
                original.cancel_requested = True
                if not original.task.done():
                    original.task.cancel()
        try:
            async with asyncio.timeout(timeout):
                while any(
                    original.active and not original.settled.is_set() and not original.setup_unknown
                    for original in self._originals.values()
                ):
                    self._changed.clear()
                    await self._changed.wait()
        except TimeoutError:
            pass
        return not any(original.active for original in self._originals.values())
