"""Observers preserve native results/cancellation and exclude private errors."""

import asyncio
import json

import pytest
from native_startup_observers import observe_media_commands, observe_standing_read

from packages.contracts.standing_work import StandingUnavailable
from packages.resources.media_processes import MediaLaunch

MARKER = "PUBLIC-SYNTHETIC-PRIVATE-DETAIL-DO-NOT-REPORT"


class Operations:
    def __init__(self):
        self.calls = 0
        self._closed = False
        self.active_operations = 0

    async def run(self):
        self.calls += 1
        return "original result"


async def test_standing_observer_returns_original_once_and_restores_method_shape():
    owner = Operations()
    original = owner.run
    with observe_standing_read(owner) as failures:
        assert await owner.run() == "original result"
    assert failures == [] and owner.calls == 1
    assert owner.run == original and "run" not in vars(owner)
    assert owner._closed is False


async def test_standing_observer_keeps_failure_and_only_reports_safe_types_and_code():
    class Failing(Operations):
        async def run(self):
            self.calls += 1
            try:
                raise ValueError(MARKER)
            except ValueError:
                raise StandingUnavailable from None

    owner = Failing()
    original = owner.run
    with pytest.raises(StandingUnavailable):
        with observe_standing_read(owner) as failures:
            await owner.run()
    assert owner.calls == 1 and owner.run == original and "run" not in vars(owner)
    assert len(failures) == 1
    assert MARKER not in json.dumps(failures)
    assert [entry["type"].split(".")[-1] for entry in failures[0]["chain"]] == [
        "StandingUnavailable",
        "ValueError",
    ]
    assert all(
        "/" not in frame["file"] for entry in failures[0]["chain"] for frame in entry["frames"]
    )


async def test_standing_cancel_restores_original_without_retry_or_failure_substitution():
    entered = asyncio.Event()

    class Held(Operations):
        async def run(self):
            self.calls += 1
            entered.set()
            await asyncio.Event().wait()

    owner = Held()
    original = owner.run
    failures = None

    async def caller():
        nonlocal failures
        with observe_standing_read(owner) as failures:
            await owner.run()

    task = asyncio.create_task(caller())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner.calls == 1 and failures == []
    assert owner.run == original and "run" not in vars(owner)


async def test_standing_observer_preserves_preexisting_instance_override():
    owner, calls, result = Operations(), [], object()

    async def override(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    owner.run = override
    with observe_standing_read(owner) as failures:
        assert await owner.run("original", action="read") is result
    assert calls == [(("original",), {"action": "read"})] and failures == []
    assert vars(owner)["run"] is override


@pytest.mark.parametrize("extra,expected", [(0, 2), (12, 8)])
async def test_standing_observer_bounds_cyclic_exception_context_and_code_frames(extra, expected):
    def deep(depth):
        if depth:
            return deep(depth - 1)
        raise ValueError(MARKER)

    class Failing(Operations):
        async def run(self):
            self.calls += 1
            try:
                deep(40)
            except ValueError as error:
                first = error
            current = first
            for _ in range(extra):
                outer = RuntimeError(MARKER)
                outer.__cause__ = current
                current = outer
            first.__context__ = current
            raise StandingUnavailable from current

    owner = Failing()
    with pytest.raises(StandingUnavailable):
        with observe_standing_read(owner) as failures:
            await owner.run()
    assert owner.calls == 1 and len(failures[0]["chain"]) == expected
    assert all(len(item["frames"]) <= 20 for item in failures[0]["chain"])
    if extra == 0:
        assert len(failures[0]["chain"][1]["frames"]) == 20
    assert MARKER not in json.dumps(failures)


@pytest.mark.parametrize(
    "raw,expected,kind",
    [
        (None, False, "unavailable"),
        (b"", False, "empty"),
        (MARKER.encode(), False, "present"),
        (b"python\0bootstrap\0nonce\0" + MARKER.encode(), True, None),
    ],
)
def test_media_observer_preserves_exact_predicate_and_single_read(
    monkeypatch, caplog, raw, expected, kind
):
    calls = []

    def read(owner, path, limit):
        calls.append((path, limit))
        return raw

    monkeypatch.setattr(MediaLaunch, "_read_observation", read)
    failures = observe_media_commands(monkeypatch)
    # Pure predicate/observer test, not native process identity or authority.
    launch = object.__new__(MediaLaunch)
    assert launch._command(123, "nonce", "bootstrap") is expected
    assert len(calls) == 1
    assert calls[0][0] == "/proc/123/cmdline"
    assert len(failures) == (not expected)
    if not expected:
        assert failures[0]["kind"] == kind
        assert failures[0]["size_bytes"] == (None if raw is None else len(raw))
    assert MARKER not in caplog.text + json.dumps(list(failures))


def test_media_observer_retains_bounded_failures_and_does_not_coerce_results(monkeypatch):
    calls = []

    class OriginalResult:
        def __bool__(self):
            pytest.fail("The observer interpreted an original result")

    result = OriginalResult()

    def command(owner, *args):
        calls.append(owner)
        return result if len(calls) == 1 else False

    monkeypatch.setattr(MediaLaunch, "_command", command)
    failures = observe_media_commands(monkeypatch)
    launch = object.__new__(MediaLaunch)
    assert launch._command(123, "nonce", "bootstrap") is result
    assert not failures
    for _ in range(70):
        assert launch._command(123, "nonce", "bootstrap") is False
    assert len(calls) == 71 and len(failures) == 64
    assert all(item == {"kind": "not_observed", "size_bytes": None} for item in failures)
