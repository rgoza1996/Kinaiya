"""Durable cross-instance owner choice and fail-closed file boundaries."""

import fcntl
import json
import os
import stat

import pytest

from services.converse.intent import ConverseIntent, IntentRecord


def write_raw(path, content):
    path.write_bytes(content)
    path.chmod(0o600)


def test_reopened_instances_share_latest_choice_and_each_request_advances_revision(tmp_path):
    path = tmp_path / "intent.json"
    first = ConverseIntent(path, initial_enabled=False)
    second = ConverseIntent(path, initial_enabled=True)
    assert second.read() == IntentRecord(False, 0)
    assert first.set(True) == IntentRecord(True, 1)
    assert second.set(False) == IntentRecord(False, 2)
    assert first.read() == IntentRecord(False, 2)
    assert first.set(False) == IntentRecord(False, 3)
    assert ConverseIntent(path).read() == IntentRecord(False, 3)
    assert path.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["intent.json", "intent.json.lock"]


def test_memory_choices_are_independent_and_records_immutable():
    first, second = ConverseIntent(), ConverseIntent(initial_enabled=False)
    assert first.set(False) == IntentRecord(False, 1)
    assert second.read() == IntentRecord(False, 0)
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        first.read().enabled = True


@pytest.mark.parametrize("value", [1, 0, "true", None, 0.0, b"false"])
def test_non_boolean_input_never_changes_record(tmp_path, value):
    path = tmp_path / "intent.json"
    intent = ConverseIntent(path)
    before = path.read_bytes()
    with pytest.raises(TypeError):
        intent.set(value)
    with pytest.raises(TypeError):
        ConverseIntent(initial_enabled=value)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"null",
        b"[]",
        b"true",
        b"{\xff}",
        b'{"enabled":false}',
        b'{"revision":0}',
        b'{"enabled":false,"revision":0,"extra":1}',
        b'{"enabled":1,"revision":0}',
        b'{"enabled":"false","revision":0}',
        b'{"enabled":false,"revision":true}',
        b'{"enabled":false,"revision":1.0}',
        b'{"enabled":false,"revision":-1}',
        b'{"enabled":false,"revision":"0"}',
        b'{"enabled":false,"revision":9223372036854775808}',
        b'{"enabled":false,"revision":0,"enabled":true}',
        b"[" * 1500 + b"]" * 1500,
        b" " * 4097,
    ],
)
def test_malformed_or_oversized_record_never_resets_to_enabled(tmp_path, raw):
    path = tmp_path / "intent.json"
    intent = ConverseIntent(path)
    write_raw(path, raw)
    for operation in (intent.read, lambda: intent.set(True), lambda: ConverseIntent(path)):
        with pytest.raises(ValueError):
            operation()
        assert path.read_bytes() == raw


def test_overflow_preserves_exact_original_bytes(tmp_path):
    path = tmp_path / "intent.json"
    raw = json.dumps({"enabled": False, "revision": (1 << 63) - 1}).encode()
    write_raw(path, raw)
    intent = ConverseIntent(path)
    with pytest.raises(OverflowError):
        intent.set(True)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("leaf", ["record", "lock"])
@pytest.mark.parametrize("kind", ["permissions", "symlink", "hardlink", "fifo"])
def test_unsafe_record_or_lock_is_not_opened_or_replaced(tmp_path, leaf, kind):
    path = tmp_path / "intent.json"
    intent = ConverseIntent(path)
    target = path if leaf == "record" else tmp_path / "intent.json.lock"
    victim = tmp_path / "victim"
    victim.write_text("unrelated synthetic data")
    victim.chmod(0o600)
    if kind == "permissions":
        target.chmod(0o644)
    elif kind == "hardlink":
        os.link(target, tmp_path / "alias")
    else:
        target.unlink()
        if kind == "symlink":
            target.symlink_to(victim)
        else:
            os.mkfifo(target, 0o600)
    before = target.lstat()
    with pytest.raises((OSError, ValueError)):
        intent.set(False)
    with pytest.raises((OSError, ValueError)):
        ConverseIntent(path)
    after = target.lstat()
    assert (before.st_ino, before.st_mode) == (after.st_ino, after.st_mode)
    assert victim.read_text() == "unrelated synthetic data"
    if leaf == "record":
        with pytest.raises((OSError, ValueError)):
            intent.read()


def test_parent_symlink_or_writable_directory_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        ConverseIntent(link / "intent.json")
    real.chmod(0o777)
    with pytest.raises(ValueError):
        ConverseIntent(real / "intent.json")
    assert not (real / "intent.json").exists()


@pytest.mark.parametrize("leaf", ["record", "lock"])
def test_foreign_uid_is_refused_without_chown(tmp_path, monkeypatch, leaf):
    import services.converse.intent as module

    path = tmp_path / "intent.json"
    intent = ConverseIntent(path)
    target = path if leaf == "record" else tmp_path / "intent.json.lock"
    inode = target.stat().st_ino
    original = module.os.fstat

    def foreign(fd):
        info = original(fd)
        if info.st_ino == inode and stat.S_ISREG(info.st_mode):
            fields = list(info)
            fields[4] = os.geteuid() + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(module.os, "fstat", foreign)
    with pytest.raises(ValueError):
        intent.set(False)


def test_busy_lock_fails_promptly_and_releases_after_failure(tmp_path):
    path = tmp_path / "intent.json"
    intent = ConverseIntent(path)
    with (tmp_path / "intent.json.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            intent.set(False)
        with pytest.raises(BlockingIOError):
            ConverseIntent(path)
        assert intent.read() == IntentRecord(True, 0)
    assert intent.set(False) == IntentRecord(False, 1)


@pytest.mark.parametrize("boundary", ["replace", "payload_fsync"])
def test_failed_atomic_update_retains_previous_choice_and_removes_temp(
    tmp_path, monkeypatch, boundary
):
    path = tmp_path / "intent.json"
    intent = ConverseIntent(path, initial_enabled=False)
    before = path.read_bytes()

    def failure(*args, **kwargs):
        raise OSError("synthetic persistence failure")

    monkeypatch.setattr(os, "replace" if boundary == "replace" else "fsync", failure)
    with pytest.raises(OSError):
        intent.set(True)
    assert path.read_bytes() == before
    assert intent.read() == IntentRecord(False, 0)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["intent.json", "intent.json.lock"]


def test_removed_record_is_never_recreated_by_read_or_set(tmp_path):
    path = tmp_path / "intent.json"
    intent = ConverseIntent(path, initial_enabled=False)
    path.unlink()
    for operation in (intent.read, lambda: intent.set(True)):
        with pytest.raises(FileNotFoundError):
            operation()
    assert not path.exists()
