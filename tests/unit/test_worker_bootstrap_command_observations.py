"""Frozen #278 observations through existing boolean/tuple and warning seams.

All process observations here are explicitly synthetic. The integration sibling
uses original native Manager processes. No test assumes a new private capture API.
"""

import asyncio
import contextvars
import errno
import threading
from collections import Counter
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from packages.resources import processes

NONCE = "private-synthetic-nonce"
BOOTSTRAP = "/private-synthetic-bootstrap/_bootstrap.py"
PAYLOAD = "private-synthetic-exception-configuration"
PARENT = 782001
PID = 782003
START = 782007
COARSE = "worker bootstrap refused: reason=%s errno=%s"
DETAIL = "worker bootstrap command observation: result=%s"
CALLER = contextvars.ContextVar("oracle_bootstrap_caller", default=None)
MATCH = NONCE.encode() + b"\0" + BOOTSTRAP.encode() + b"\0"
MISSING = {
    "read_failed": OSError(errno.EACCES, PAYLOAD),
    "empty_read": b"",
    "missing_nonce": BOOTSTRAP.encode() + b"\0",
    "missing_bootstrap": NONCE.encode() + b"\0",
    "missing_both": b"unrelated\0tokens\0",
}


class BadToken:
    def decode(self, encoding, errors):
        assert (encoding, errors) == ("utf-8", "replace")
        raise ValueError(PAYLOAD)


class DecodeFailure(bytes):
    def split(self, separator):
        assert separator == b"\0"
        return [BadToken()]


class SplitFailure(bytes):
    def split(self, separator):
        raise RuntimeError(PAYLOAD)


@dataclass
class Native:
    pid: int
    commands: list = field(default_factory=lambda: [MATCH, MATCH])
    before: object = None
    after: object = None
    binding: object = None
    stat_count: int = 0
    command_count: int = 0
    events: list = field(default_factory=list)
    open_error: object = None
    close_error: object = None

    def __post_init__(self):
        self.before = self.after = ("S", PARENT, self.pid, self.pid, START)
        self.binding = self.pid


@pytest.fixture
def native(monkeypatch):
    state = SimpleNamespace(rows={}, warnings=[], warning_hook=None, read_hook=None, stat_hook=None)

    def row(pid=PID):
        if pid not in state.rows:
            state.rows[pid] = Native(pid)
        return state.rows[pid]

    state.row = row

    def raise_if_error(value):
        if isinstance(value, BaseException):
            raise value
        return value

    def stat(pid):
        value = row(pid)
        value.events.append("stat")
        value.stat_count += 1
        assert value.stat_count <= 2, "No additional stat observation"
        if state.stat_hook is not None:
            state.stat_hook(value)
        return raise_if_error(value.before if value.stat_count == 1 else value.after)

    def fd_open(pid, flags):
        value = row(pid)
        assert flags == 0
        value.events.append("pidfd_open")
        raise_if_error(value.open_error)
        return pid + 100

    def fd_binding(fd):
        value = row(fd - 100)
        value.events.append("binding")
        return raise_if_error(value.binding)

    def fd_close(fd):
        value = row(fd - 100)
        value.events.append("pidfd_close")
        raise_if_error(value.close_error)

    def forbidden(*args, **kwargs):
        pytest.fail("Diagnostic observations cannot signal, write grant or adopt")

    class Read:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            self.value.events.append("cmd_enter")
            return self

        def read(self, bound):
            value = self.value
            value.events.append(("cmd_read", bound))
            assert bound == 65536
            value.command_count += 1
            assert value.command_count <= 2, "Only the existing helper calls may read"
            if state.read_hook is not None:
                state.read_hook(value)
            result = raise_if_error(value.commands[value.command_count - 1])
            return result[:bound] if type(result) is bytes else result

        def __exit__(self, *args):
            self.value.events.append("cmd_close")

    def open_command(path, mode):
        assert mode == "rb" and path.startswith("/proc/") and path.endswith("/cmdline")
        value = row(int(path.split("/")[2]))
        value.events.append("cmd_open")
        return Read(value)

    def warning(message, *args, **kwargs):
        label = CALLER.get()
        state.warnings.append((label, message, args, kwargs))
        if label in state.rows:
            row(label).events.append(("warning", message))
        if state.warning_hook is not None:
            state.warning_hook(label, message, args)

    monkeypatch.setattr(processes, "open", open_command, raising=False)
    monkeypatch.setattr(processes, "_proc_stat_full", stat)
    monkeypatch.setattr(processes, "_pidfd_pid", fd_binding)
    monkeypatch.setattr(processes, "_pidfd_send", forbidden)
    monkeypatch.setattr(processes, "_adopt_member", forbidden)
    monkeypatch.setattr(processes, "signal", SimpleNamespace(pidfd_send_signal=forbidden))
    monkeypatch.setattr(
        processes,
        "os",
        SimpleNamespace(getpid=lambda: PARENT, pidfd_open=fd_open, close=fd_close, write=forbidden),
    )
    monkeypatch.setattr(processes.LOG, "warning", warning)

    def verify(pid=PID):
        token = CALLER.set(pid)
        try:
            return processes._verify_bootstrap(pid, NONCE, BOOTSTRAP)
        finally:
            CALLER.reset(token)

    state.verify = verify
    return state


def messages(native, *, label=PID, kind=None):
    records = [item for item in native.warnings if item[0] == label]
    for _, message, args, kwargs in records:
        assert message in {COARSE, DETAIL}
        assert kwargs == {}
        assert all(type(arg) is str for arg in args)
        rendered = message % args
        for private in (NONCE, BOOTSTRAP, PAYLOAD, str(PID), str(PARENT), str(START)):
            assert private not in rendered
    return [(msg, args) for _, msg, args, _ in records if kind is None or msg == kind]


@pytest.mark.parametrize("phase", ["pre", "post"])
@pytest.mark.parametrize("detail", [*MISSING, "decode_failed"])
def test_existing_single_read_classifies_each_closed_command_failure(native, phase, detail):
    row = native.row()
    row.commands[phase == "post"] = (
        DecodeFailure(b"nonempty") if detail == "decode_failed" else MISSING[detail]
    )
    assert native.verify() == (None, None)
    assert messages(native, kind=COARSE) == [
        (COARSE, (phase + "_command_mismatch", "not_reported"))
    ]
    assert messages(native, kind=DETAIL) == [(DETAIL, (detail,))]
    count = 1 if phase == "pre" else 2
    assert row.command_count == count
    assert row.events.count("cmd_open") == row.events.count("cmd_close") == count
    assert [
        event for event in row.events if isinstance(event, tuple) and event[0] == "cmd_read"
    ] == [("cmd_read", 65536)] * count
    if phase == "post":
        assert row.events.count("pidfd_close") == 1
        close = row.events.index("pidfd_close")
        assert all(
            index > close
            for index, event in enumerate(row.events)
            if isinstance(event, tuple) and event[0] == "warning"
        )
    else:
        assert "pidfd_open" not in row.events and "pidfd_close" not in row.events


@pytest.mark.parametrize(
    "raw", [b"\0", b"\0\0", b"\xff\0", b" " + NONCE.encode() + b"\0", NONCE.encode() + b"suffix\0"]
)
def test_nonempty_nul_tokens_and_replacement_decode_are_not_empty_read(native, raw):
    native.row().commands[0] = raw
    assert native.verify() == (None, None)
    expected = "missing_bootstrap" if NONCE.encode() in raw.split(b"\0") else "missing_both"
    assert messages(native, kind=DETAIL) == [(DETAIL, (expected,))]


@pytest.mark.parametrize(
    "raw", [MATCH, b"\xff\0" + MATCH, BOOTSTRAP.encode() + b"\0" + NONCE.encode(), MATCH + MATCH]
)
def test_exact_tokens_replacement_utf8_and_success_are_silent(native, raw):
    native.row().commands = [raw, raw]
    assert native.verify() == (PID + 100, START)
    assert native.warnings == []
    assert native.row().command_count == 2
    assert "pidfd_close" not in native.row().events


@pytest.mark.parametrize(
    "raw,expected", [(b"x" * 65536 + MATCH, False), (MATCH + b"x" * 65536, True)]
)
def test_helper_uses_only_one_original_bounded_read_without_logging(native, raw, expected):
    native.row().commands = [raw]
    assert processes._cmdline_has(PID, NONCE, BOOTSTRAP) is expected
    assert native.row().command_count == 1
    assert native.row().events == ["cmd_open", "cmd_enter", ("cmd_read", 65536), "cmd_close"]
    assert native.warnings == []


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_open_failure_is_read_failed_without_an_added_read(native, monkeypatch, phase):
    original = processes.open
    calls = []

    def faulty(path, mode):
        calls.append(path)
        if len(calls) == (1 if phase == "pre" else 2):
            raise OSError(errno.EACCES, PAYLOAD)
        return original(path, mode)

    monkeypatch.setattr(processes, "open", faulty)
    assert native.verify() == (None, None)
    assert len(calls) == (1 if phase == "pre" else 2)
    assert messages(native, kind=DETAIL) == [(DETAIL, ("read_failed",))]
    assert native.row().command_count == (0 if phase == "pre" else 1)


@pytest.mark.parametrize(
    "first", ["post_stat_unavailable", "pidfd_binding_unavailable", "pidfd_binding_mismatch"]
)
def test_earlier_native_predicate_precedence_does_not_emit_later_command_detail(native, first):
    row = native.row()
    row.commands[1] = b""
    if first == "post_stat_unavailable":
        row.after = None
    else:
        row.binding = None if first.endswith("unavailable") else PID + 1
    assert native.verify() == (None, None)
    assert messages(native, kind=COARSE) == [(COARSE, (first, "not_reported"))]
    assert messages(native, kind=DETAIL) == []
    assert row.command_count == 2 and row.events.count("pidfd_close") == 1


@pytest.mark.parametrize(
    "reason,field,value",
    [
        ("post_not_live", 0, "Z"),
        ("post_parent_mismatch", 1, PARENT + 1),
        ("post_group_mismatch", 2, PID + 1),
        ("post_session_mismatch", 3, PID + 1),
        ("post_starttime_mismatch", 4, START + 1),
    ],
)
def test_matched_commands_do_not_label_other_original_refusal_predicates(
    native, reason, field, value
):
    row = native.row()
    after = list(row.after)
    after[field] = value
    row.after = tuple(after)
    assert native.verify() == (None, None)
    assert messages(native, kind=COARSE) == [(COARSE, (reason, "not_reported"))]
    assert messages(native, kind=DETAIL) == []
    assert row.command_count == 2 and row.events.count("pidfd_close") == 1


@pytest.mark.parametrize("earlier", ["pre_stat", "pre_parent", "post_exception"])
def test_prior_authority_failures_never_acquire_stale_command_detail(native, earlier):
    assert processes._cmdline_has(PID + 1, NONCE, BOOTSTRAP) is True
    row = native.row()
    if earlier == "pre_stat":
        row.before, reason = None, "pre_stat_unavailable"
    elif earlier == "pre_parent":
        row.before, reason = ("S", PARENT + 1, PID, PID, START), "pre_parent_mismatch"
    else:
        row.after, reason = RuntimeError(PAYLOAD), "post_observation_error"
    assert native.verify() == (None, None)
    assert messages(native, kind=COARSE) == [(COARSE, (reason, "not_reported"))]
    assert messages(native, kind=DETAIL) == []


@pytest.mark.parametrize("seed", ["direct", "prior_verifier"])
def test_missing_capture_does_not_invent_a_prior_helper_observation(native, monkeypatch, seed):
    native.row(PID + 1).commands[0] = MISSING["missing_nonce"]
    if seed == "direct":
        assert processes._cmdline_has(PID + 1, NONCE, BOOTSTRAP) is False
        assert native.warnings == []
    else:
        assert native.verify(PID + 1) == (None, None)
    monkeypatch.setattr(processes, "_cmdline_has", lambda *args: False)
    assert native.verify() == (None, None)
    assert messages(native, kind=COARSE) == [(COARSE, ("pre_command_mismatch", "not_reported"))]
    assert messages(native, kind=DETAIL) == []


@pytest.mark.parametrize("seed", ["direct", "prior_verifier"])
def test_direct_coarse_helper_has_no_authority_to_reuse_a_command_capture(native, seed):
    native.row().commands[0] = MISSING["missing_nonce"]
    if seed == "direct":
        assert processes._cmdline_has(PID, NONCE, BOOTSTRAP) is False
    else:
        assert native.verify() == (None, None)
    native.warnings.clear()
    assert processes._bootstrap_refused("pre_command_mismatch") == (None, None)
    assert messages(native, label=None) == [(COARSE, ("pre_command_mismatch", "not_reported"))]


def test_reentrant_verifier_from_diagnostic_sink_keeps_each_own_observation(native):
    native.row().commands[0] = MISSING["missing_nonce"]
    native.row(PID + 1).commands[0] = MISSING["missing_bootstrap"]
    entered = False

    def nested(label, message, args):
        nonlocal entered
        if label == PID and message == COARSE and not entered:
            entered = True
            assert native.verify(PID + 1) == (None, None)

    native.warning_hook = nested
    assert native.verify() == (None, None)
    assert entered
    assert messages(native, kind=DETAIL) == [(DETAIL, ("missing_nonce",))]
    assert messages(native, label=PID + 1, kind=DETAIL) == [(DETAIL, ("missing_bootstrap",))]


def test_nested_direct_helper_cannot_replace_original_post_command_capture(native):
    row = native.row()
    row.commands[1] = MISSING["missing_bootstrap"]
    native.row(PID + 1).commands[0] = MISSING["missing_nonce"]

    def nested(value):
        if value.pid == PID and value.stat_count == 2:
            assert processes._cmdline_has(PID + 1, NONCE, BOOTSTRAP) is False

    native.stat_hook = nested
    assert native.verify() == (None, None)
    assert row.command_count == 2
    assert messages(native, kind=DETAIL) == [(DETAIL, ("missing_bootstrap",))]


def test_concurrent_threads_do_not_use_another_verifier_last_result(native):
    native.row().commands[0] = MISSING["missing_nonce"]
    native.row(PID + 1).commands[0] = MISSING["missing_bootstrap"]
    first_coarse, second_finished = threading.Event(), threading.Event()
    results, errors = {}, []

    def hook(label, message, args):
        if label == PID and message == COARSE:
            first_coarse.set()
            assert second_finished.wait(5), "Original verifier peer must finish"

    def run(pid):
        try:
            if pid != PID:
                assert first_coarse.wait(5)
            results[pid] = native.verify(pid)
        except BaseException as error:
            errors.append(error)
        finally:
            if pid != PID:
                second_finished.set()

    native.warning_hook = hook
    threads = [threading.Thread(target=run, args=(pid,)) for pid in (PID, PID + 1)]
    try:
        for thread in threads:
            thread.start()
    finally:
        for thread in threads:
            thread.join(6)
    assert not errors and all(not thread.is_alive() for thread in threads)
    assert results == {PID: (None, None), PID + 1: (None, None)}
    assert messages(native, kind=DETAIL) == [(DETAIL, ("missing_nonce",))]
    assert messages(native, label=PID + 1, kind=DETAIL) == [(DETAIL, ("missing_bootstrap",))]


async def test_async_contexts_do_not_inherit_a_direct_helper_capture(native, monkeypatch):
    native.row(PID + 1).commands[0] = MISSING["missing_nonce"]
    seed_done, peer_done = asyncio.Event(), asyncio.Event()
    real_helper = processes._cmdline_has

    def helper(pid, nonce, bootstrap):
        return False if pid == PID else real_helper(pid, nonce, bootstrap)

    monkeypatch.setattr(processes, "_cmdline_has", helper)

    async def first():
        assert real_helper(PID + 1, NONCE, BOOTSTRAP) is False
        seed_done.set()
        await peer_done.wait()

    async def second():
        await seed_done.wait()
        try:
            assert native.verify() == (None, None)
            assert messages(native, kind=DETAIL) == []
        finally:
            peer_done.set()

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    finally:
        peer_done.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("phase", ["pre", "post"])
@pytest.mark.parametrize("sink", [COARSE, DETAIL])
def test_hostile_diagnostic_sink_cannot_change_tuple_or_native_close(native, phase, sink):
    native.row().commands[phase == "post"] = MISSING["missing_both"]

    def hostile(label, message, args):
        if message == sink:
            raise RuntimeError(PAYLOAD)

    native.warning_hook = hostile
    assert native.verify() == (None, None)
    assert native.row().command_count == (1 if phase == "pre" else 2)
    assert native.row().events.count("pidfd_close") == (phase == "post")
    messages(native)


@pytest.mark.parametrize(
    "where", ["read_non_os", "split_non_os", "pre_stat", "pidfd_open", "pidfd_close"]
)
def test_native_exception_identity_and_original_propagation_remain(native, where):
    error = RuntimeError(PAYLOAD)
    row = native.row()
    if where == "read_non_os":
        row.commands[0] = error
    elif where == "split_non_os":
        row.commands[0] = SplitFailure(b"nonempty")
    elif where == "pre_stat":
        row.before = error
    elif where == "pidfd_open":
        row.open_error = error
    else:
        row.commands[1], row.close_error = b"", error
    with pytest.raises(RuntimeError) as caught:
        native.verify()
    if where != "split_non_os":
        assert caught.value is error
    assert native.warnings == []
    assert row.events.count("pidfd_close") == (where == "pidfd_close")


def test_successful_verifier_does_not_touch_a_hostile_logger(native):
    native.warning_hook = lambda *args: pytest.fail("Success stays silent")
    assert native.verify() == (PID + 100, START)
    assert native.warnings == []
    assert Counter(native.row().events)["cmd_close"] == 2
