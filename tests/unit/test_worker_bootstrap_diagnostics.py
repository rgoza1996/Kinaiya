"""Fault probes for diagnostic-only worker bootstrap refusal observations (#176)."""

import errno
import logging
import os
from types import SimpleNamespace

import pytest

from packages.resources import processes

_PID = 920003
_PARENT = 920001
_FD = 940007
_START = 918273
_NONCE = "synthetic-private-nonce-marker"
_BOOTSTRAP = "/synthetic-private-path-marker/_bootstrap.py"
_PAYLOAD = "synthetic-private-exception-config-marker"
_FORMAT = "worker bootstrap refused: reason=%s errno=%s"
_READS = ["stat", "cmd", "pidfd_open", "stat", "binding", "cmd"]
_REASONS = (
    "pre_stat_unavailable",
    "pre_not_live",
    "pre_parent_mismatch",
    "pre_group_mismatch",
    "pre_session_mismatch",
    "pre_command_mismatch",
    "pidfd_open_failed",
    "post_observation_error",
    "post_stat_unavailable",
    "pidfd_binding_unavailable",
    "pidfd_binding_mismatch",
    "post_command_mismatch",
    "post_not_live",
    "post_parent_mismatch",
    "post_group_mismatch",
    "post_session_mismatch",
    "post_starttime_mismatch",
)


def _stat(*, state="S", parent=_PARENT, group=_PID, session=_PID, start=_START):
    return state, parent, group, session, start


class _UnprintableOSError(OSError):
    def __str__(self):
        raise AssertionError("Diagnostic must not render an exception")

    def __repr__(self):
        raise AssertionError("Diagnostic must not render an exception")


@pytest.fixture
def probe(monkeypatch, caplog):
    real_close = os.close
    state = SimpleNamespace(
        events=[],
        before=_stat(),
        after=_stat(),
        command=[True, True],
        bound=_PID,
        open_error=None,
        close_error=None,
        stat_count=0,
        command_count=0,
        opened=[],
        closed=[],
    )

    def observed(value):
        if isinstance(value, BaseException):
            raise value
        return value

    def stat(pid):
        assert pid == _PID
        state.events.append("stat")
        state.stat_count += 1
        assert state.stat_count <= 2, "No retry or extra process observation is allowed"
        return observed(state.before if state.stat_count == 1 else state.after)

    def command(pid, nonce, path):
        assert (pid, nonce, path) == (_PID, _NONCE, _BOOTSTRAP)
        state.events.append("cmd")
        state.command_count += 1
        assert state.command_count <= 2, "No retry or extra command observation is allowed"
        return observed(state.command[state.command_count - 1])

    def binding(fd):
        assert fd == _FD
        state.events.append("binding")
        return observed(state.bound)

    def open_fd(pid, flags):
        assert (pid, flags) == (_PID, 0)
        state.events.append("pidfd_open")
        if state.open_error is not None:
            raise state.open_error
        state.opened.append(_FD)
        return _FD

    def close_fd(fd):
        assert fd == _FD
        state.events.append("close")
        state.closed.append(fd)
        if state.close_error is not None:
            raise state.close_error

    def forbidden(*args, **kwargs):
        pytest.fail("A bootstrap diagnostic must not signal, grant or adopt a process")

    # Patching the module binding avoids changing the runner's global os.close.
    monkeypatch.setattr(
        processes,
        "os",
        SimpleNamespace(
            getpid=lambda: _PARENT,
            pidfd_open=open_fd,
            close=close_fd,
            write=forbidden,
            kill=forbidden,
            killpg=forbidden,
        ),
    )
    monkeypatch.setattr(processes, "signal", SimpleNamespace(pidfd_send_signal=forbidden))
    monkeypatch.setattr(processes, "_proc_stat_full", stat)
    monkeypatch.setattr(processes, "_cmdline_has", command)
    monkeypatch.setattr(processes, "_pidfd_pid", binding)
    monkeypatch.setattr(processes, "_pidfd_send", forbidden)
    monkeypatch.setattr(processes, "_adopt_member", forbidden)
    caplog.set_level(logging.WARNING, logger=processes.LOG.name)

    class WarningOrder(logging.Handler):
        def emit(self, record):
            state.events.append("warning")

    handler = WarningOrder()
    processes.LOG.addHandler(handler)
    try:
        yield state
    finally:
        processes.LOG.removeHandler(handler)
        assert os.close is real_close


def _verify():
    return processes._verify_bootstrap(_PID, _NONCE, _BOOTSTRAP)


def _warning(caplog, reason, category="not_reported"):
    records = [record for record in caplog.records if record.name == processes.LOG.name]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.msg == _FORMAT
    assert record.args == (reason, category)
    assert all(type(value) is str for value in record.args)
    assert record.exc_info is None and record.stack_info is None
    message = record.getMessage()
    assert message == f"worker bootstrap refused: reason={reason} errno={category}"
    assert all(
        value not in message
        for value in (_NONCE, _BOOTSTRAP, _PAYLOAD, str(_PID), str(_PARENT), str(_FD), str(_START))
    )


def _fault(probe, reason):
    def change(stat, field, value):
        values = list(stat)
        values[field] = value
        return tuple(values)

    if reason == "pre_stat_unavailable":
        probe.before = None
    elif reason == "pre_not_live":
        probe.before = change(probe.before, 0, "Z")
    elif reason == "pre_parent_mismatch":
        probe.before = change(probe.before, 1, _PARENT + 1)
    elif reason == "pre_group_mismatch":
        probe.before = change(probe.before, 2, _PID + 1)
    elif reason == "pre_session_mismatch":
        probe.before = change(probe.before, 3, _PID + 1)
    elif reason == "pre_command_mismatch":
        probe.command[0] = False
    elif reason == "pidfd_open_failed":
        probe.open_error = OSError(_PAYLOAD)
    elif reason == "post_observation_error":
        probe.after = RuntimeError(_PAYLOAD)
    elif reason == "post_stat_unavailable":
        probe.after = None
    elif reason == "pidfd_binding_unavailable":
        probe.bound = None
    elif reason == "pidfd_binding_mismatch":
        probe.bound = _PID + 1
    elif reason == "post_command_mismatch":
        probe.command[1] = False
    elif reason == "post_not_live":
        probe.after = change(probe.after, 0, "Z")
    elif reason == "post_parent_mismatch":
        probe.after = change(probe.after, 1, _PARENT + 1)
    elif reason == "post_group_mismatch":
        probe.after = change(probe.after, 2, _PID + 1)
    elif reason == "post_session_mismatch":
        probe.after = change(probe.after, 3, _PID + 1)
    elif reason == "post_starttime_mismatch":
        probe.after = change(probe.after, 4, _START + 1)
    else:
        raise AssertionError("Unknown test fault")


@pytest.mark.parametrize("reason", _REASONS)
def test_every_existing_refusal_predicate_has_one_constant_reason(probe, caplog, reason):
    _fault(probe, reason)
    assert _verify() == (None, None)
    _warning(caplog, reason)
    if reason.startswith("pre_"):
        assert probe.opened == probe.closed == []
        expected = ["stat", "cmd"] if reason == "pre_command_mismatch" else ["stat"]
    elif reason == "pidfd_open_failed":
        assert probe.opened == probe.closed == []
        expected = _READS[:3]
    else:
        assert probe.opened == probe.closed == [_FD]
        expected = (_READS[:4] if reason == "post_observation_error" else _READS) + ["close"]
    assert probe.events == expected + ["warning"]


@pytest.mark.parametrize("state", ["R", "S", "D", "T", "t", "I"])
def test_accepted_tuple_retains_handle_and_exact_observation_order(probe, caplog, state):
    probe.before = probe.after = _stat(state=state)
    assert _verify() == (_FD, _START)
    assert probe.events == _READS
    assert probe.opened == [_FD] and probe.closed == []
    assert not caplog.records


@pytest.mark.parametrize("state", ["Z", "X", "x"])
@pytest.mark.parametrize("phase", ["pre", "post"])
def test_every_existing_nonlive_state_still_refuses_without_grant(probe, caplog, state, phase):
    setattr(probe, "before" if phase == "pre" else "after", _stat(state=state))
    assert _verify() == (None, None)
    _warning(caplog, phase + "_not_live")
    expected = ["stat"] if phase == "pre" else _READS + ["close"]
    assert probe.events == expected + ["warning"]
    assert probe.closed == ([] if phase == "pre" else [_FD])


@pytest.mark.parametrize(
    ("first", "later"),
    [
        ("pre_stat_unavailable", "pre_command_mismatch"),
        ("pre_not_live", "pre_command_mismatch"),
        ("pre_not_live", "pre_parent_mismatch"),
        ("pre_parent_mismatch", "pre_command_mismatch"),
        ("pre_parent_mismatch", "pre_group_mismatch"),
        ("pre_group_mismatch", "pre_command_mismatch"),
        ("pre_group_mismatch", "pre_session_mismatch"),
        ("pre_session_mismatch", "pre_command_mismatch"),
        ("post_stat_unavailable", "pidfd_binding_mismatch"),
        ("pidfd_binding_unavailable", "post_command_mismatch"),
        ("pidfd_binding_mismatch", "post_command_mismatch"),
        ("post_command_mismatch", "post_not_live"),
        ("post_not_live", "post_starttime_mismatch"),
        ("post_not_live", "post_parent_mismatch"),
        ("post_parent_mismatch", "post_starttime_mismatch"),
        ("post_parent_mismatch", "post_group_mismatch"),
        ("post_group_mismatch", "post_starttime_mismatch"),
        ("post_group_mismatch", "post_session_mismatch"),
        ("post_session_mismatch", "post_starttime_mismatch"),
    ],
)
def test_first_existing_failed_predicate_wins_over_later_defects(probe, caplog, first, later):
    _fault(probe, later)
    _fault(probe, first)
    assert _verify() == (None, None)
    _warning(caplog, first)
    if first.startswith("pre_"):
        assert probe.opened == probe.closed == []
    else:
        assert probe.events == _READS + ["close", "warning"]
        assert probe.closed == [_FD]


@pytest.mark.parametrize("observation", ["stat", "binding", "cmd"])
def test_postopen_observation_exceptions_close_before_fixed_warning(probe, caplog, observation):
    error = _UnprintableOSError(errno.EACCES, _PAYLOAD, _BOOTSTRAP)
    if observation == "stat":
        probe.after = error
        expected = _READS[:4]
    elif observation == "binding":
        probe.bound = error
        expected = _READS[:5]
    else:
        probe.command[1] = error
        expected = _READS
    assert _verify() == (None, None)
    assert probe.events == expected + ["close", "warning"]
    assert probe.closed == [_FD]
    _warning(caplog, "post_observation_error")


@pytest.mark.parametrize(
    ("number", "category"),
    [
        (errno.EACCES, "access_denied"),
        (errno.EPERM, "operation_denied"),
        (errno.ESRCH, "process_missing"),
        (errno.EMFILE, "process_fd_limit"),
        (errno.ENFILE, "system_fd_limit"),
        (errno.ENOSYS, "not_supported"),
        (errno.EINVAL, "invalid_argument"),
        (errno.ENOMEM, "memory_unavailable"),
        (0, "other"),
        (-1, "other"),
        (987654, "other"),
        (None, "not_reported"),
        (True, "not_reported"),
        (False, "not_reported"),
        (13.0, "not_reported"),
        ("EACCES", "not_reported"),
    ],
)
def test_pidfd_open_errno_is_a_closed_category_without_exception_payload(
    probe, caplog, number, category
):
    error = _UnprintableOSError(_PAYLOAD)
    error.errno = number
    probe.open_error = error
    assert _verify() == (None, None)
    assert probe.events == _READS[:3] + ["warning"]
    assert probe.opened == probe.closed == []
    _warning(caplog, "pidfd_open_failed", category)


@pytest.mark.parametrize("reason", [reason for reason in _REASONS if reason != "pidfd_open_failed"])
def test_nonopen_reasons_do_not_inspect_an_error_errno(probe, caplog, reason):
    class ErrnoTrap:
        @property
        def errno(self):
            pytest.fail("Only the pidfd-open refusal may inspect errno")

        def __str__(self):
            pytest.fail("Never format diagnostic error objects")

    assert processes._bootstrap_refused(reason, ErrnoTrap()) == (None, None)
    _warning(caplog, reason)
    assert probe.events == ["warning"]
    assert probe.opened == probe.closed == []


class _ReasonSubclass(str):
    pass


@pytest.mark.parametrize(
    "reason",
    [None, False, 42, [], {}, _PAYLOAD, _ReasonSubclass("pre_stat_unavailable")],
    ids=["none", "bool", "integer", "list", "dict", "unlisted-string", "str-subclass"],
)
def test_invalid_reason_values_use_unknown_without_formatting_input(probe, caplog, reason):
    assert processes._bootstrap_refused(reason, _UnprintableOSError(errno.EACCES, _PAYLOAD)) == (
        None,
        None,
    )
    _warning(caplog, "unknown")
    assert probe.events == ["warning"]


def test_hostile_reason_object_is_not_hashed_compared_or_rendered(probe, caplog):
    class HostileReason:
        def __hash__(self):
            pytest.fail("Unvalidated reason must not be hashed")

        def __eq__(self, other):
            pytest.fail("Unvalidated reason must not be compared")

        def __str__(self):
            pytest.fail("Unvalidated reason must not be rendered")

    assert processes._bootstrap_refused(HostileReason()) == (None, None)
    _warning(caplog, "unknown")


@pytest.mark.parametrize("failure", [RuntimeError, OSError, TypeError])
@pytest.mark.parametrize(
    "reason", ["pre_command_mismatch", "pidfd_open_failed", "post_starttime_mismatch"]
)
def test_ordinary_logger_failure_cannot_change_refusal_or_close_order(
    probe, monkeypatch, failure, reason
):
    _fault(probe, reason)

    def broken_warning(*args, **kwargs):
        probe.events.append("warning")
        raise failure(_PAYLOAD)

    monkeypatch.setattr(processes.LOG, "warning", broken_warning)
    assert _verify() == (None, None)
    if reason == "pre_command_mismatch":
        assert probe.events == ["stat", "cmd", "warning"]
        assert probe.opened == probe.closed == []
    elif reason == "pidfd_open_failed":
        assert probe.events == _READS[:3] + ["warning"]
        assert probe.opened == probe.closed == []
    else:
        assert probe.events == _READS + ["close", "warning"]
        assert probe.opened == probe.closed == [_FD]


def test_accepted_tuple_does_not_depend_on_logger_availability(probe, monkeypatch):
    def broken_warning(*args, **kwargs):
        pytest.fail("An accepted bootstrap must not emit a refusal diagnostic")

    monkeypatch.setattr(processes.LOG, "warning", broken_warning)
    assert _verify() == (_FD, _START)
    assert probe.events == _READS and probe.closed == []


def test_ordinary_errno_attribute_failure_cannot_escape_verifier(probe, caplog):
    class ErrnoError(OSError):
        @property
        def errno(self):
            raise RuntimeError(_PAYLOAD)

    probe.open_error = ErrnoError(_PAYLOAD)
    assert _verify() == (None, None)
    assert probe.opened == probe.closed == []
    assert probe.events[:3] == _READS[:3]
    for record in caplog.records:
        assert _PAYLOAD not in record.getMessage()


def test_ignored_os_close_failure_still_has_one_attempt_before_warning(probe, caplog):
    probe.after = None
    probe.close_error = _UnprintableOSError(errno.EBADF, _PAYLOAD)
    assert _verify() == (None, None)
    assert probe.events == _READS + ["close", "warning"]
    assert probe.closed == [_FD]
    _warning(caplog, "post_stat_unavailable")


@pytest.mark.parametrize("where", ["pre_stat", "pre_cmd", "non_os_open", "non_os_close"])
def test_existing_nondiagnostic_exception_propagation_is_preserved(probe, caplog, where):
    error = RuntimeError(_PAYLOAD)
    if where == "pre_stat":
        probe.before = error
        expected = ["stat"]
    elif where == "pre_cmd":
        probe.command[0] = error
        expected = _READS[:2]
    elif where == "non_os_open":
        probe.open_error = error
        expected = _READS[:3]
    else:
        probe.after = None
        probe.close_error = error
        expected = _READS + ["close"]
    with pytest.raises(RuntimeError) as caught:
        _verify()
    assert caught.value is error
    assert probe.events == expected
    assert not caplog.records
    assert probe.closed == ([_FD] if where == "non_os_close" else [])
