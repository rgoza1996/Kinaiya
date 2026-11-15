"""#620: refusal observations preserve the original media identity boundary."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from media_process_helpers import _WAIT, _binding
from media_process_helpers import harness as harness

from packages.resources import media_processes

PID = 972013
PARENT = 972011
START = 918273
NONCE = "synthetic-private-media-nonce"
BOOTSTRAP = "/synthetic-private-media-path/_media_bootstrap.py"
DETAIL = "synthetic-private-media-exception"
FORMAT = "media bootstrap refused: reason=%s"
LOG = logging.getLogger("packages.resources.media_processes")


def stat(**changes):
    fields = dict(state="S", parent=PARENT, group=PID, session=PID, start=START)
    fields.update(changes)
    return tuple(fields.values())


@pytest.fixture
def probe(monkeypatch, caplog):
    value = SimpleNamespace(
        before=stat(),
        after=stat(),
        commands=[True, True],
        bound=PID,
        owner=object(),
        events=[],
        stats=0,
        command_reads=0,
        close_error=None,
    )
    launch = media_processes.MediaLaunch.__new__(media_processes.MediaLaunch)

    def observed(result):
        if isinstance(result, BaseException):
            raise result
        return result

    def read_stat(pid):
        assert pid == PID
        value.events.append("stat")
        value.stats += 1
        assert value.stats <= 2
        return observed(value.before if value.stats == 1 else value.after)

    def command(pid, nonce, bootstrap):
        assert (pid, nonce, bootstrap) == (PID, NONCE, BOOTSTRAP)
        value.events.append("command")
        value.command_reads += 1
        assert value.command_reads <= 2
        return observed(value.commands[value.command_reads - 1])

    def pidfd(pid):
        assert pid == PID
        value.events.append("pidfd")
        return observed(value.owner)

    def binding(owner):
        assert owner is value.owner
        value.events.append("binding")
        return observed(value.bound)

    def close(owner):
        assert owner is value.owner
        value.events.append("close")
        if value.close_error:
            raise value.close_error
        return True

    launch._stat, launch._command = read_stat, command
    launch._pidfd, launch._pidfd_pid, launch._close = pidfd, binding, close
    monkeypatch.setattr(media_processes, "os", SimpleNamespace(getpid=lambda: PARENT))
    caplog.set_level(logging.WARNING, logger=LOG.name)

    class Order(logging.Handler):
        def emit(self, record):
            value.events.append("warning")

    handler = Order()
    LOG.addHandler(handler)
    try:
        yield launch, value
    finally:
        LOG.removeHandler(handler)


@pytest.mark.parametrize(
    "changes,reason,operations",
    [
        ({"before": None}, "pre_stat_unavailable", ["stat"]),
        ({"before": stat(state="Z")}, "pre_not_live", ["stat"]),
        ({"before": stat(parent=0)}, "pre_parent_mismatch", ["stat"]),
        ({"before": stat(group=0)}, "pre_group_mismatch", ["stat"]),
        ({"before": stat(session=0)}, "pre_session_mismatch", ["stat"]),
        ({"commands": [False, False]}, "pre_command_mismatch", ["stat", "command"]),
        ({"owner": None}, "pidfd_unavailable", ["stat", "command", "pidfd"]),
        ({"after": None}, "post_stat_unavailable", ["stat", "command", "pidfd", "stat", "close"]),
        (
            {"bound": None},
            "pidfd_binding_mismatch",
            ["stat", "command", "pidfd", "stat", "binding", "close"],
        ),
        (
            {"bound": 0},
            "pidfd_binding_mismatch",
            ["stat", "command", "pidfd", "stat", "binding", "close"],
        ),
        (
            {"commands": [True, False], "after": stat(state="Z")},
            "post_command_mismatch",
            ["stat", "command", "pidfd", "stat", "binding", "command", "close"],
        ),
        (
            {"after": stat(state="Z")},
            "post_not_live",
            ["stat", "command", "pidfd", "stat", "binding", "command", "close"],
        ),
        (
            {"after": stat(parent=0)},
            "post_parent_mismatch",
            ["stat", "command", "pidfd", "stat", "binding", "command", "close"],
        ),
        (
            {"after": stat(group=0)},
            "post_group_mismatch",
            ["stat", "command", "pidfd", "stat", "binding", "command", "close"],
        ),
        (
            {"after": stat(session=0)},
            "post_session_mismatch",
            ["stat", "command", "pidfd", "stat", "binding", "command", "close"],
        ),
        (
            {"after": stat(start=START + 1)},
            "post_starttime_mismatch",
            ["stat", "command", "pidfd", "stat", "binding", "command", "close"],
        ),
    ],
)
def test_first_existing_guard_and_exact_read_close_order(
    probe, caplog, changes, reason, operations
):
    launch, value = probe
    for key, selected in changes.items():
        setattr(value, key, selected)
    assert launch._verify_bootstrap(PID, NONCE, BOOTSTRAP) == (None, None)
    assert value.events == [*operations, "warning"]
    records = [r for r in caplog.records if r.name == LOG.name]
    assert len(records) == 1
    assert records[0].msg == FORMAT and records[0].args == (reason,)
    text = records[0].getMessage()
    assert all(
        private not in text
        for private in [str(PID), str(PARENT), str(START), NONCE, BOOTSTRAP, DETAIL]
    )


def test_success_returns_original_owner_and_start_without_warning(probe, caplog):
    launch, value = probe
    assert launch._verify_bootstrap(PID, NONCE, BOOTSTRAP) == (value.owner, START)
    assert value.events == ["stat", "command", "pidfd", "stat", "binding", "command"]
    assert not [r for r in caplog.records if r.name == LOG.name]


@pytest.mark.parametrize("location", ["before", "after", "bound"])
def test_native_observation_exceptions_propagate_and_keep_original_close(probe, caplog, location):
    launch, value = probe
    error = RuntimeError(DETAIL)
    setattr(value, location, error)
    with pytest.raises(RuntimeError) as raised:
        launch._verify_bootstrap(PID, NONCE, BOOTSTRAP)
    assert raised.value is error
    assert value.events.count("close") == (0 if location == "before" else 1)
    assert not [r for r in caplog.records if r.name == LOG.name]


def test_original_close_exception_is_not_converted_to_a_refusal_warning(probe, caplog):
    launch, value = probe
    value.after = None
    value.close_error = RuntimeError(DETAIL)
    with pytest.raises(RuntimeError) as raised:
        launch._verify_bootstrap(PID, NONCE, BOOTSTRAP)
    assert raised.value is value.close_error
    assert value.events.count("close") == 1
    assert not [r for r in caplog.records if r.name == LOG.name]


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_diagnostic_sink_cannot_change_refusal_or_post_open_close(probe, monkeypatch, failure):
    launch, value = probe
    value.after = None

    def hostile(*args):
        assert value.events[-1] == "close"
        raise failure(DETAIL)

    monkeypatch.setattr(LOG, "warning", hostile)
    assert launch._verify_bootstrap(PID, NONCE, BOOTSTRAP) == (None, None)
    assert value.events.count("close") == 1


@pytest.mark.parametrize("failure_read", [1, 2])
async def test_actual_ungranted_child_refusal_never_executes_target(
    harness, monkeypatch, caplog, failure_read
):
    pool, marker = harness.pool()
    binding = _binding()
    launch = harness.register(pool, binding)
    upload, grant = harness.grant(launch, binding)
    original = launch._command
    count = 0

    def command(*args):
        nonlocal count
        count += 1
        return False if count == failure_read else original(*args)

    monkeypatch.setattr(launch, "_command", command)
    caplog.set_level(logging.WARNING, logger=LOG.name)
    with pytest.raises(media_processes.MediaProcessError) as raised:
        await asyncio.wait_for(pool.start(launch, grant), _WAIT)
    assert raised.value.code == "unavailable"
    assert count == failure_read and len(harness.processes) == 1
    assert not launch._granted and not marker.exists()
    assert (await pool.settle(launch)).state == "settled"
    assert await asyncio.wait_for(harness.processes[0].wait(), _WAIT) == 0
    assert not marker.exists()
    assert upload.delete() and upload.deleted
    records = [r for r in caplog.records if r.name == LOG.name]
    assert [r.args for r in records] == [
        ("pre_command_mismatch" if failure_read == 1 else "post_command_mismatch",)
    ]
