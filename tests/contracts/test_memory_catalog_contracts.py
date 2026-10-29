import pytest
from pydantic import ValidationError

from packages.contracts.memory import MemoryState
from packages.contracts.memory_catalog import MemoryCatalogResult, MemorySummary


def summary(**changes):
    return dict(
        node_id="note-1",
        ordinal=1,
        kind="assertion",
        state="active",
        preview="A synthetic note",
        preview_truncated=False,
        **changes,
    )


def state(count=1):
    return MemoryState(
        session_id="session",
        thread_id="thread",
        epoch=1,
        enabled=False,
        revision=0,
        node_count=count,
        status="ready",
    )


def test_empty_and_complete_catalog():
    assert MemoryCatalogResult(state=state(0), entries=[]).entries == []
    row = MemorySummary(**summary())
    assert MemoryCatalogResult(state=state(), entries=[row]).entries == [row]


@pytest.mark.parametrize(
    "changes",
    [
        {"preview": "😀" * 33},
        {"preview": ""},
        {"preview": "\ud800"},
        {"preview": "a\x00b"},
        {"preview": "a\x7fb"},
        {"preview": 1},
        {"preview_truncated": 1},
        {"ordinal": True},
        {"ordinal": 1.0},
        {"extra": "refused"},
        {"state": "forgotten"},
        {"preview": None},
    ],
)
def test_malformed_summary(changes):
    with pytest.raises(ValidationError):
        MemorySummary.model_validate(summary() | changes)


def test_complete_utf8_boundary_and_redaction():
    assert MemorySummary.model_validate(summary() | {"preview": "😀" * 32}).preview
    # The prefix of a valid nonblank body can itself contain only whitespace.
    assert MemorySummary.model_validate(summary() | {"preview": " \n\t"}).preview
    redacted = summary() | {"state": "forgotten", "preview": None}
    assert MemorySummary.model_validate(redacted).preview is None
    with pytest.raises(ValidationError):
        MemorySummary.model_validate(redacted | {"preview_truncated": True})


@pytest.mark.parametrize(
    "rows,count",
    [
        ([summary()], 0),
        ([summary() | {"ordinal": 2}], 1),
        ([summary(), summary() | {"ordinal": 2}], 2),
        ([summary() | {"ordinal": 2}, summary() | {"node_id": "note-2"}], 2),
    ],
)
def test_partial_duplicate_or_unordered_catalog_refused(rows, count):
    with pytest.raises(ValidationError):
        MemoryCatalogResult(state=state(count), entries=rows)


def test_retained_namespace_bound():
    rows = [summary() | {"node_id": f"note-{i}", "ordinal": i} for i in range(1, 257)]
    assert len(MemoryCatalogResult(state=state(256), entries=rows).entries) == 256
    with pytest.raises(ValidationError):
        MemoryCatalogResult(
            state=state(257),
            entries=rows
            + [
                summary()
                | {
                    "node_id": "note-257",
                    "ordinal": 257,
                }
            ],
        )
