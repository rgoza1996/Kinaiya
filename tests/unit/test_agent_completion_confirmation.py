"""Public synthetic receipt/bounds tests, adapted after Muse candidate review."""

import asyncio

import pytest

from packages.contracts.models import AppendRequest, JournalEntry
from services.agent import completion


@pytest.fixture
def request_body():
    return AppendRequest(
        session_id="synthetic-session",
        thread_id="synthetic-thread",
        epoch=1,
        kind="agent.outcome",
        task_refs=["synthetic-task"],
        causes=[],
        idempotency_key="synthetic-completion-key",
        payload={
            "state": "succeeded",
            "objective_revision": 1,
            "task": {"task_id": "synthetic-task", "state": "succeeded"},
        },
    )


def entry(body, sequence=11, **updates):
    return JournalEntry(
        **body.model_dump(), sequence=sequence, origin="agent-supervisor"
    ).model_copy(deep=True, update=updates)


class Journal:
    def __init__(self, *, ack=None, pages=None, append_error=None, read_error=None):
        self.ack = ack
        self.pages = list(pages or [])
        self.append_error, self.read_error = append_error, read_error
        self.appends, self.reads = [], []

    async def append(self, body):
        self.appends.append(body)
        if self.append_error is not None:
            raise self.append_error
        return self.ack

    async def read(self, **query):
        self.reads.append(query)
        if self.read_error is not None:
            raise self.read_error
        return self.pages.pop(0) if self.pages else []


async def test_exact_ack_does_not_read_or_retry(request_body):
    journal = Journal(ack=entry(request_body))
    assert await completion.append_completion(journal, request_body, after=10)
    assert journal.appends == [request_body] and journal.reads == []


async def test_commit_lost_ack_confirms_exact_native_readback(request_body):
    journal = Journal(
        append_error=RuntimeError("synthetic lost ACK"), pages=[[entry(request_body)]]
    )
    assert await completion.append_completion(journal, request_body, after=10)
    assert journal.appends == [request_body]
    assert journal.reads == [
        {
            "after": 10,
            "limit": 200,
            "session_id": request_body.session_id,
            "thread_id": request_body.thread_id,
            "kinds": ["agent.outcome"],
            "task_id": request_body.task_refs[0],
        }
    ]


@pytest.mark.parametrize("read_error", [None, RuntimeError("synthetic private transport text")])
async def test_empty_or_failed_readback_means_unconfirmed(request_body, read_error):
    journal = Journal(append_error=RuntimeError("synthetic secret"), read_error=read_error)
    assert not await completion.append_completion(journal, request_body, after=10)
    assert len(journal.appends) == len(journal.reads) == 1


@pytest.mark.parametrize(
    "updates",
    [
        {"origin": "user"},
        {"origin": "Agent_Supervisor"},
        {"origin": ""},
        {"kind": "agent.tool"},
        {"session_id": "other"},
        {"thread_id": "other"},
        {"epoch": 2},
        {"task_refs": ["other"]},
        {"causes": ["other"]},
        {"idempotency_key": "other"},
        {"sequence": 10},
        {"sequence": True},
        {"payload": {"state": "succeeded", "objective_revision": 2}},
        {
            "payload": {
                "state": "succeeded",
                "objective_revision": True,
                "task": {"task_id": "synthetic-task", "state": "succeeded"},
            }
        },
        {
            "payload": {
                "state": "succeeded",
                "objective_revision": 1,
                "task": {"task_id": "synthetic-task", "state": "failed"},
            }
        },
    ],
)
async def test_mismatched_ack_and_readback_never_confirm(request_body, updates):
    bad = entry(request_body, **updates)
    journal = Journal(ack=bad, pages=[[bad]])
    assert not await completion.append_completion(journal, request_body, after=10)
    assert len(journal.appends) == 1


async def test_plain_dictionary_missing_origin_is_not_a_native_receipt(request_body):
    bad = entry(request_body).model_dump(exclude={"origin"})
    journal = Journal(ack=bad, pages=[[bad]])
    assert not await completion.append_completion(journal, request_body, after=10)
    assert len(journal.appends) == 1


async def test_pagination_finds_exact_match_without_reappending(request_body):
    first = entry(request_body, idempotency_key="another-outcome")
    journal = Journal(append_error=RuntimeError(), pages=[[first], [entry(request_body, 12)]])
    assert await completion.append_completion(journal, request_body, after=10)
    assert [query["after"] for query in journal.reads] == [10, 11]
    assert len(journal.appends) == 1


@pytest.mark.parametrize("page_kind", ["not-list", "oversized", "decreasing", "trailing-invalid"])
async def test_malformed_whole_page_cannot_confirm(request_body, page_kind):
    good = entry(request_body)
    page = {
        "not-list": (good,),
        "oversized": [entry(request_body, i) for i in range(11, 212)],
        "decreasing": [good, entry(request_body, 10)],
        "trailing-invalid": [good, {"sequence": 12}],
    }[page_kind]
    journal = Journal(append_error=RuntimeError(), pages=[page])
    assert not await completion.append_completion(journal, request_body, after=10)
    assert len(journal.appends) == 1 and len(journal.reads) == 1


async def test_readback_stops_at_declared_page_bound(request_body):
    pages = [[entry(request_body, i, idempotency_key="other")] for i in range(11, 19)]
    journal = Journal(append_error=RuntimeError(), pages=[*pages, [entry(request_body, 19)]])
    assert not await completion.append_completion(journal, request_body, after=10)
    assert len(journal.reads) == completion.READBACK_MAX_PAGES == 8
    assert len(journal.appends) == 1 and len(journal.pages) == 1


async def test_single_readback_deadline_and_no_retry(request_body, monkeypatch):
    gate = asyncio.Event()
    cancelled = asyncio.Event()

    class HungRead(Journal):
        async def read(self, **query):
            self.reads.append(query)
            try:
                await gate.wait()
            finally:
                cancelled.set()

    monkeypatch.setattr(completion, "READBACK_TIMEOUT_S", 0.01)
    journal = HungRead(append_error=RuntimeError())
    assert not await completion.append_completion(journal, request_body, after=10)
    assert cancelled.is_set()
    assert len(journal.appends) == len(journal.reads) == 1


@pytest.mark.parametrize("phase", ["append", "read"])
async def test_cancellation_propagates_without_another_write(request_body, phase):
    journal = Journal(
        append_error=asyncio.CancelledError() if phase == "append" else RuntimeError(),
        read_error=asyncio.CancelledError(),
    )
    with pytest.raises(asyncio.CancelledError):
        await completion.append_completion(journal, request_body, after=10)
    assert len(journal.appends) == 1
    assert len(journal.reads) == (0 if phase == "append" else 1)


@pytest.mark.parametrize("after", [-1, True, False, None, 0.1, "10"])
async def test_invalid_cursor_performs_no_io(request_body, after):
    journal = Journal(ack=entry(request_body))
    assert not await completion.append_completion(journal, request_body, after=after)
    assert journal.appends == journal.reads == []


@pytest.mark.parametrize(
    "updates", [{"kind": "agent.tool"}, {"task_refs": []}, {"task_refs": ["a", "b"]}]
)
async def test_invalid_request_performs_no_io(request_body, updates):
    journal = Journal(ack=entry(request_body))
    invalid = request_body.model_copy(update=updates)
    assert not await completion.append_completion(journal, invalid, after=10)
    assert journal.appends == journal.reads == []
