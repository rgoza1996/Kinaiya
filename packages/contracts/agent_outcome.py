"""Bounded native outcomes referencing an immutable prefix of tool receipts.

The journal remains the evidence store. A prefix commitment is not work or
success proof: readers must restore and validate every original receipt before
using the Task. Legacy inline outcomes keep their original representation.
"""

import asyncio
import hashlib
import json
import re

from packages.contracts.models import TERMINAL_STATES, AppendRequest, Evidence, JournalEntry, Task

MAX_OUTCOME_BYTES = 65_536
MAX_EVIDENCE_COUNT = 2_000
MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_PAGES = 256
EVIDENCE_PAGE_LIMIT = 200
EVIDENCE_READ_TIMEOUT_S = 10.0
_CONTRACT = "agent.evidence-prefix.v1"
_SCOPES = {"public_synthetic": 0, "private_cloud_authorized": 1, "local_only": 2}
_REFERENCE_FIELDS = {
    "contract",
    "task_id",
    "session_id",
    "thread_id",
    "epoch",
    "objective_revision",
    "after_sequence",
    "through_sequence",
    "evidence_count",
    "evidence_bytes",
    "evidence_sha256",
    "receipts_sha256",
    "content_scope",
}


class OutcomeEvidenceUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Native outcome evidence is unconfirmed or unavailable")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _feed(digest, value):
    encoded = _canonical(value)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    return len(encoded)


def _binding(task):
    return (task.task_id, task.session_id, task.thread_id, task.epoch)


def _reference_header(outcome):
    task = Task.model_validate(outcome.payload["task"])
    reference = outcome.payload["evidence_ref"]
    if (
        task.state not in TERMINAL_STATES
        or type(reference) is not dict
        or set(reference) != _REFERENCE_FIELDS
        or reference["contract"] != _CONTRACT
        or any(
            type(reference[key]) is not int
            for key in (
                "epoch",
                "objective_revision",
                "after_sequence",
                "through_sequence",
                "evidence_count",
                "evidence_bytes",
            )
        )
        or not 0 <= reference["evidence_count"] <= MAX_EVIDENCE_COUNT
        or not 0 <= reference["evidence_bytes"] <= MAX_EVIDENCE_BYTES
        or not 1 <= reference["after_sequence"] <= reference["through_sequence"] < outcome.sequence
        or any(
            type(reference[key]) is not str or re.fullmatch(r"[a-f0-9]{64}", reference[key]) is None
            for key in ("evidence_sha256", "receipts_sha256")
        )
        or reference["content_scope"] not in _SCOPES
    ):
        raise OutcomeEvidenceUnavailable
    return task, reference


def _evidence_digest(evidence):
    digest, size = hashlib.sha256(), 0
    if len(evidence) > MAX_EVIDENCE_COUNT:
        raise OutcomeEvidenceUnavailable
    for receipt in evidence:
        if not isinstance(receipt, Evidence):
            raise OutcomeEvidenceUnavailable
        size += _feed(digest, receipt.model_dump(mode="json"))
        if size > MAX_EVIDENCE_BYTES:
            raise OutcomeEvidenceUnavailable
    return digest.hexdigest(), size


class OutcomeEvidenceIndex:
    """Native acknowledged receipts only; hashes avoid copying tool output.

    Runtime appends populate this after an exact authenticated acknowledgement.
    Recovery populates it from ordered authenticated journal rows. The original
    Task evidence list is separately checked before a sparse outcome is frozen.
    """

    def __init__(self, task: Task, after_sequence: int):
        if type(after_sequence) is not int or after_sequence < 1:
            raise OutcomeEvidenceUnavailable
        self.binding = _binding(task)
        self.after_sequence = self.through_sequence = after_sequence
        self.count = self.byte_count = 0
        self.scope = task.content_scope
        self._evidence = hashlib.sha256()
        self._receipts = hashlib.sha256()

    def observe(self, entry: JournalEntry):
        if (
            not isinstance(entry, JournalEntry)
            or entry.origin != "agent-supervisor"
            or entry.kind != "agent.tool"
            or (entry.task_refs, entry.session_id, entry.thread_id, entry.epoch)
            != ([self.binding[0]], *self.binding[1:])
            or entry.payload.get("task_id") != self.binding[0]
            or type(entry.sequence) is not int
            or entry.sequence <= self.through_sequence
            or "evidence" not in entry.payload
        ):
            raise OutcomeEvidenceUnavailable
        try:
            evidence = Evidence.model_validate(entry.payload["evidence"])
            scope = entry.payload.get("content_scope", "local_only")
            if scope not in _SCOPES:
                raise OutcomeEvidenceUnavailable
            encoded = evidence.model_dump(mode="json")
            size = len(_canonical(encoded))
            if self.count >= MAX_EVIDENCE_COUNT or self.byte_count + size > MAX_EVIDENCE_BYTES:
                raise OutcomeEvidenceUnavailable
            _feed(self._evidence, encoded)
            _feed(self._receipts, [entry.sequence, entry.event_id, encoded])
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise OutcomeEvidenceUnavailable from None
        self.count += 1
        self.byte_count += size
        self.through_sequence = entry.sequence
        self.scope = max((self.scope, scope), key=_SCOPES.__getitem__)
        return evidence

    def observe_ack(self, request: AppendRequest, entry: JournalEntry):
        if not isinstance(entry, JournalEntry) or _canonical(
            entry.model_dump(mode="json", include=set(AppendRequest.model_fields))
        ) != _canonical(request.model_dump(mode="json")):
            raise OutcomeEvidenceUnavailable
        return self.observe(entry)

    def reference(self, task: Task):
        if _binding(task) != self.binding or task.content_scope not in _SCOPES:
            raise OutcomeEvidenceUnavailable
        digest, size = _evidence_digest(task.evidence)
        if (
            len(task.evidence) != self.count
            or size != self.byte_count
            or digest != self._evidence.hexdigest()
            or _SCOPES[task.content_scope] < _SCOPES[self.scope]
            or any(e.objective_revision > task.objective_revision for e in task.evidence)
        ):
            raise OutcomeEvidenceUnavailable
        return {
            "contract": _CONTRACT,
            "task_id": task.task_id,
            "session_id": task.session_id,
            "thread_id": task.thread_id,
            "epoch": task.epoch,
            "objective_revision": task.objective_revision,
            "after_sequence": self.after_sequence,
            "through_sequence": self.through_sequence,
            "evidence_count": self.count,
            "evidence_bytes": self.byte_count,
            "evidence_sha256": digest,
            "receipts_sha256": self._receipts.hexdigest(),
            "content_scope": self.scope,
        }


def bounded_outcome_request(
    request: AppendRequest, task: Task, index: OutcomeEvidenceIndex, *, allow_sparse=True
):
    """Keep small historical shape; replace oversized duplicate evidence once."""
    if len(request.model_dump_json().encode("utf-8")) <= MAX_OUTCOME_BYTES:
        return request
    if not allow_sparse:
        # Standing admission has a different native submit proof. Do not emit
        # an ordinary-submit commitment that its consumer cannot authenticate.
        raise OutcomeEvidenceUnavailable
    reference = index.reference(task)
    payload = dict(request.payload)
    snapshot = dict(payload["task"])
    snapshot["evidence"] = []
    payload.update(task=snapshot, evidence=[], evidence_ref=reference)
    result = request.model_copy(deep=True, update={"payload": payload})
    if len(result.model_dump_json().encode("utf-8")) > MAX_OUTCOME_BYTES:
        # Metadata is never cropped to fit. No POST was made by this helper.
        raise OutcomeEvidenceUnavailable
    return result


def restore_outcome_task(outcome: JournalEntry, index: OutcomeEvidenceIndex, evidence):
    """Validate the entire commitment before installing a recovered Task."""
    try:
        task, reference = _reference_header(outcome)
        if (
            outcome.origin != "agent-supervisor"
            or outcome.kind != "agent.outcome"
            or outcome.task_refs != [task.task_id]
            or (outcome.session_id, outcome.thread_id, outcome.epoch)
            != (task.session_id, task.thread_id, task.epoch)
            or outcome.payload.get("task_id") != task.task_id
            or outcome.payload.get("objective_revision") != task.objective_revision
            or outcome.payload.get("state") != task.state.value
            or outcome.payload.get("content_scope") != task.content_scope
            or task.evidence != []
            or outcome.payload.get("evidence") != []
            or type(reference) is not dict
            or type(outcome.sequence) is not int
            or outcome.sequence <= index.through_sequence
        ):
            raise OutcomeEvidenceUnavailable
        task.evidence = list(evidence)
        expected = index.reference(task)
        # Canonical equality also distinguishes bool from int in commitments.
        if _canonical(reference) != _canonical(expected):
            raise OutcomeEvidenceUnavailable
        return task
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise OutcomeEvidenceUnavailable from None


async def hydrate_outcome_task(journal, outcome: JournalEntry):
    """Bounded authenticated reads only; missing proof never becomes empty evidence."""
    try:
        task, reference = _reference_header(outcome)
        after = reference["after_sequence"]
        through = reference["through_sequence"]
        if (
            type(after) is not int
            or type(through) is not int
            or not 1 <= after <= through < outcome.sequence
        ):
            raise OutcomeEvidenceUnavailable
        index = OutcomeEvidenceIndex(task, after)
        # Tool scopes are folded from their original rows, not the narrowed
        # terminal snapshot. This seed is fixed by the native submit below.
        submit = await journal.read(
            after=after - 1,
            limit=1,
            session_id=task.session_id,
            thread_id=task.thread_id,
            kinds=["agent.command"],
            task_id=task.task_id,
        )
        if (
            not isinstance(submit, list)
            or len(submit) != 1
            or not isinstance(submit[0], JournalEntry)
            or submit[0].sequence != after
            or submit[0].origin != "agent-supervisor"
            or submit[0].kind != "agent.command"
            or (submit[0].session_id, submit[0].thread_id, submit[0].epoch)
            != (task.session_id, task.thread_id, task.epoch)
            or submit[0].task_refs != [task.task_id]
            or submit[0].payload.get("task_id") != task.task_id
            or submit[0].payload.get("op") != "submit"
            or submit[0].payload.get("idempotency_key") != task.idempotency_key
        ):
            raise OutcomeEvidenceUnavailable
        index.scope = submit[0].payload.get("content_scope", "local_only")
        if index.scope not in _SCOPES:
            raise OutcomeEvidenceUnavailable
        evidence, cursor = [], after
        for _ in range(MAX_EVIDENCE_PAGES):
            if cursor >= through:
                return restore_outcome_task(outcome, index, evidence)
            page = await journal.read(
                after=cursor,
                limit=EVIDENCE_PAGE_LIMIT,
                session_id=task.session_id,
                thread_id=task.thread_id,
                kinds=["agent.tool"],
                task_id=task.task_id,
            )
            if not isinstance(page, list) or not 1 <= len(page) <= EVIDENCE_PAGE_LIMIT:
                raise OutcomeEvidenceUnavailable
            previous = cursor
            for entry in page:
                if (
                    not isinstance(entry, JournalEntry)
                    or type(entry.sequence) is not int
                    or entry.sequence <= previous
                ):
                    raise OutcomeEvidenceUnavailable
                previous = entry.sequence
            for entry in page:
                if entry.sequence > through:
                    break
                if entry.origin == "agent-supervisor" and "evidence" in entry.payload:
                    evidence.append(index.observe(entry))
            cursor = previous
            await asyncio.sleep(0)
        raise OutcomeEvidenceUnavailable
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
        raise OutcomeEvidenceUnavailable from None


async def read_outcome_task(journal, outcome: JournalEntry):
    owner = asyncio.current_task()
    cancellations = owner.cancelling()
    async with asyncio.timeout(EVIDENCE_READ_TIMEOUT_S) as deadline:
        task = await hydrate_outcome_task(journal, outcome)
        # A cancellation-resistant adapter returning valid bytes cannot grant
        # late announcement authority. An external stop takes precedence.
        if owner.cancelling() > cancellations + int(deadline.expired()):
            raise asyncio.CancelledError
        if deadline.expired():
            raise TimeoutError
        if owner.cancelling() > cancellations:
            raise asyncio.CancelledError
        return task
