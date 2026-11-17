"""Native v2 Agent handoffs; actual Agent execution and permissions stay independent."""

import asyncio
import secrets
from dataclasses import dataclass, field

from packages.contracts.live_decisions_attempts import LiveDecisionsOutcome
from packages.contracts.live_effect_pending import LiveEffectPendingItem, LiveEffectPendingQuery
from packages.contracts.live_effects import (
    PROTOCOL,
    LiveEffectAdmit,
    LiveEffectFinish,
    LiveEffectRefused,
    LiveEffectStart,
    effect_key,
)
from packages.contracts.models import InterruptRequest, SteerRequest, Task, TaskRequest
from packages.contracts.operation_ownership import ActualOperation, actual_owner


@dataclass
class _Route:
    item: LiveEffectPendingItem
    admission: LiveEffectAdmit
    task: asyncio.Task | None = None
    admit_task: asyncio.Task | None = None
    start_task: asyncio.Task | None = None
    start_request: LiveEffectStart | None = None
    finish_task: asyncio.Task | None = None
    finish_request: LiveEffectFinish | None = None
    dispatched: bool = False
    retired: bool = False
    evidence_after: int = 0
    owners: list = field(default_factory=list)


class LiveAgentDispatcher:
    """Passive construction; one original call per start, no restart replay."""

    def __init__(self, journal, agent):
        self.journal, self.agent = journal, agent
        self.owner_instance = secrets.token_hex(16)
        self.routes = {}
        self.after_sequence = 0
        self.closed = False
        self.last_error = None
        self.running = False
        self.ready = False
        self.run_task = None
        self.run_owner = None

    async def process(self, item, *, timeout=2):
        item = LiveEffectPendingItem.model_validate(item, strict=True)
        if self.closed:
            return None
        route = self.routes.get(item.event_id)
        if route is None:
            if len(self.routes) >= 64:
                raise RuntimeError("Original live Agent handoffs retain their capacity")
            route = _Route(
                item=item,
                admission=LiveEffectAdmit(
                    **item.query("agent_route").model_dump(mode="json"),
                    owner_instance=self.owner_instance,
                    operation_id=secrets.token_hex(16),
                ),
            )
            self.routes[item.event_id] = route
        elif route.item != item:
            raise ValueError("Live Agent delivery changed its original native outcome")
        if route.task is None or route.task.done():
            route.owners[:] = [
                owner for owner in route.owners if not (owner.finished and not owner.failed)
            ]
            owner = ActualOperation(lambda _: None)
            route.owners.append(owner)

            async def original():
                with actual_owner(owner):
                    return await self._advance(route)

            route.task = asyncio.create_task(original())
            owner.retain_task(route.task)
            route.task.add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )
        try:
            return await asyncio.wait_for(asyncio.shield(route.task), timeout)
        except TimeoutError:
            return None

    async def step(self):
        if self.closed:
            return False
        # Revisit retained originals even when their row is no longer pending:
        # a completion may have committed before its acknowledgment was lost.
        for event_id, route in tuple(self.routes.items()):
            if route.retired and all(owner.finished and not owner.failed for owner in route.owners):
                self.routes.pop(event_id)
            elif route.task is None or route.task.done():
                await self.process(route.item)
        query = LiveEffectPendingQuery(
            schema_version="2.0",
            protocol=PROTOCOL,
            effect="agent_route",
            after_sequence=self.after_sequence,
            limit=32,
        )
        page = await self.journal.pending_live_effects(query)
        for item in page.items:
            await self.process(item)
        self.after_sequence = 0 if page.exhausted else page.through_sequence
        return not page.exhausted

    async def run(self):
        if self.run_task is not None and not self.run_task.done():
            raise RuntimeError("The original live Agent consumer already owns its loop")
        self.run_task = asyncio.current_task()
        self.run_owner = ActualOperation(lambda _: None)
        self.run_owner.retain_task(self.run_task)
        self.running = True
        try:
            with actual_owner(self.run_owner):
                while not self.closed:
                    try:
                        before = await self.journal.journal_wake_cursor()
                        backlog = await self.step()
                        after = await self.journal.journal_wake_cursor()
                        self.ready = True
                        if self.closed:
                            break
                        if not backlog and before == after:
                            await self.journal.wait_for_journal_change(after)
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                        self.ready = False
                        if not self.closed:
                            await asyncio.sleep(0.1)  # Error recovery, never input pacing.
        finally:
            self.running = False
            self.ready = False

    async def _lookup(self, route):
        return (await self.journal.live_effect_lookup(route.item.query("agent_route"))).record

    async def _advance(self, route):
        state = await self.journal.lookup_live_decision(route.item.publication)
        entry = state.receipt
        if entry is None or (entry.event_id, entry.sequence) != (
            route.item.event_id,
            route.item.sequence,
        ):
            raise ValueError("Live Agent handoff has no original native publication")
        outcome = LiveDecisionsOutcome.model_validate(entry.payload, strict=True)
        record = await self._lookup(route)
        if record is None:
            if self.closed:
                return None
            if route.admit_task is None:
                route.admit_task = asyncio.create_task(
                    self.journal.live_effect_admit(route.admission)
                )
            try:
                record = await asyncio.shield(route.admit_task)
            except LiveEffectRefused:
                route.retired = True  # Positive native refusal, not an unknown handoff.
                return None
            except Exception:
                record = await self._lookup(route)
                if record is None:
                    return None  # Absence does not authorize another write.
        if record.completion is not None and record.completion.status != "uncertain":
            route.retired = True
            return record
        if record.started is not None and (
            route.start_request is None
            or record.started.original != route.start_request
            or route.dispatched
        ):
            return await self._reconcile(route, record)
        if record.started is None:
            if self.closed:
                return None
            if outcome.decision.agent_route == "QUEUE_UNTIL_TASK_DONE":
                tasks = await self.agent.status()
                if not isinstance(tasks, list) or any(not isinstance(task, Task) for task in tasks):
                    raise ValueError("Native Agent status is unavailable")
                target_id = outcome.decision.target_task_id
                if target_id is not None:
                    target = next((task for task in tasks if task.task_id == target_id), None)
                    if (
                        target is None
                        or (target.session_id, target.thread_id, target.epoch)
                        != (entry.session_id, entry.thread_id, entry.epoch)
                        or (
                            outcome.decision.expected_objective_revision is not None
                            and target.objective_revision
                            != outcome.decision.expected_objective_revision
                        )
                    ):
                        self.last_error = "native_queue_target_unavailable"
                        return record
                    if getattr(target.state, "value", target.state) not in {
                        "succeeded",
                        "failed",
                        "cancelled",
                    }:
                        return record
                if any(
                    (task.session_id, task.thread_id, task.epoch)
                    == (entry.session_id, entry.thread_id, entry.epoch)
                    and getattr(task.state, "value", task.state)
                    not in {"succeeded", "failed", "cancelled"}
                    for task in tasks
                ):
                    return record
            if route.start_request is None:
                route.start_request = LiveEffectStart(
                    schema_version="2.0",
                    protocol=PROTOCOL,
                    original=record.receipt,
                    owner_instance=self.owner_instance,
                    operation_id=secrets.token_hex(16),
                )
            if route.start_task is None:
                route.start_task = asyncio.create_task(
                    self.journal.live_effect_start(route.start_request)
                )
            try:
                record = await asyncio.shield(route.start_task)
            except LiveEffectRefused:
                route.start_task = None
                # This completed refusal made no start. The exact metadata
                # identity may retry after its blocker changes; uncertain
                # requests and actual Agent operations are never repeated.
                return await self._lookup(route)
            except Exception:
                record = await self._lookup(route)
                if (
                    record is None
                    or record.started is None
                    or record.started.original != route.start_request
                ):
                    return None
        if self.closed:
            return await self._finish(route, record, "declined", "owner_stopped_before_dispatch")
        source = await self.journal.entry(outcome.source.original.source_event_id)
        outcome.source.check_entry(source)
        key = effect_key(record.receipt)
        common = dict(
            source_event_ids=[source.event_id],
            source_revision=outcome.source.original.source_revision,
            idempotency_key=key,
        )
        route.dispatched = True  # Before the first await that can enter Agent.
        try:
            if outcome.decision.agent_route == "QUEUE_UNTIL_TASK_DONE":
                await self.agent.submit(
                    TaskRequest(
                        session_id=source.session_id,
                        thread_id=source.thread_id,
                        epoch=source.epoch,
                        objective=source.payload["text"],
                        content_scope=source.payload["content_scope"],
                        **common,
                    ),
                    actor="decisions",
                )
            elif outcome.decision.agent_route == "STEER_CURRENT_TASK":
                await self.agent.steer(
                    outcome.decision.target_task_id,
                    SteerRequest(
                        expected_objective_revision=outcome.decision.expected_objective_revision,
                        instruction=source.payload["text"],
                        content_scope=source.payload["content_scope"],
                        source_event_ids=[source.event_id],
                        idempotency_key=key,
                    ),
                    actor="decisions",
                )
            elif outcome.decision.agent_route == "INTERRUPT_CURRENT_TASK":
                await self.agent.interrupt(
                    outcome.decision.target_task_id,
                    InterruptRequest(
                        expected_objective_revision=outcome.decision.expected_objective_revision,
                        reason=source.payload["text"][:2000],
                        **common,
                    ),
                    actor="decisions",
                )
            else:
                raise ValueError("Native outcome does not select an Agent route")
        except Exception as exc:
            self.last_error = type(exc).__name__
        return await self._reconcile(route, record)

    async def _reconcile(self, route, record):
        if record.started is None:
            return record
        observed = await self.journal.live_effect_evidence(record.receipt)
        if observed.evidence is not None:
            return await self._finish(
                route,
                record,
                "recorded",
                "native_agent_command_recorded",
                observed.evidence.model_dump(mode="json"),
            )
        if record.completion is None:
            return await self._finish(
                route, record, "uncertain", "native_agent_command_unconfirmed"
            )
        return record

    async def _finish(self, route, record, status, reason, evidence=None):
        request = LiveEffectFinish(
            schema_version="2.0",
            protocol=PROTOCOL,
            original=record.receipt,
            status=status,
            reason_code=reason,
            evidence=evidence,
        )
        if route.finish_request != request:
            if route.finish_request is not None and not (
                route.finish_request.status == "uncertain" and status == "recorded"
            ):
                return await self._lookup(route)
            route.finish_request = request
            route.finish_task = asyncio.create_task(self.journal.live_effect_finish(request))
        try:
            result = await asyncio.shield(route.finish_task)
        except Exception:
            result = await self._lookup(route)
        if (
            result is not None
            and result.completion is not None
            and result.completion.status != "uncertain"
        ):
            route.retired = True
        return result

    async def close(self):
        self.closed = True
        pending = [
            route.task
            for route in self.routes.values()
            if route.task is not None and not route.task.done()
        ]
        if (
            self.run_task is not None
            and not self.run_task.done()
            and self.run_task is not asyncio.current_task()
        ):
            pending.append(self.run_task)
        if pending:
            await asyncio.wait(pending, timeout=2)
        return (
            self.run_owner is None or self.run_owner.finished and not self.run_owner.failed
        ) and all(
            owner.finished and not owner.failed
            for route in self.routes.values()
            for owner in route.owners
        )
