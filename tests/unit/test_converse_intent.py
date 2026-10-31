"""Durable cross-instance owner choice and fail-closed file boundaries."""

import fcntl
import json
import os
import stat
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from services.converse.intent import ConverseIntent, IntentRecord

MAX_REVISION = (1 << 63) - 1


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


@pytest.mark.parametrize("durable", [False, True])
def test_cas_stale_choice_returns_immutable_observation_without_advancing(tmp_path, durable):
    from services.converse import intent as module

    path = tmp_path / "intent.json" if durable else None
    intent = ConverseIntent(path)
    observed = intent.read()
    accepted = intent.set(False, expected_revision=observed.revision)
    assert accepted == IntentRecord(False, 1)
    before = path.read_bytes() if path else None
    with pytest.raises(ValueError) as caught:
        intent.set(True, expected_revision=observed.revision)
    assert isinstance(caught.value, module.OwnerIntentConflict)
    assert caught.value.observed == accepted
    assert isinstance(caught.value.observed, IntentRecord)
    with pytest.raises(FrozenInstanceError):
        caught.value.observed.revision = 99
    assert intent.read() == accepted
    if path:
        assert path.read_bytes() == before
    assert intent.set(False, expected_revision=accepted.revision) == IntentRecord(False, 2)
    # A receipt describes the refused transition, even after another owner write.
    assert caught.value.observed == accepted


def test_cas_shared_instances_same_value_writer_invalidates_snapshot_without_a_write(tmp_path):
    from services.converse import intent as module

    path = tmp_path / "intent.json"
    restore = ConverseIntent(path, initial_enabled=False)
    owner = ConverseIntent(path)
    snapshot = restore.read()
    assert owner.set(False) == IntentRecord(False, 1)
    before_bytes, before_stat = path.read_bytes(), path.stat()
    with pytest.raises(ValueError) as caught:
        restore.set(True, expected_revision=snapshot.revision)
    assert isinstance(caught.value, module.OwnerIntentConflict)
    assert caught.value.observed == IntentRecord(False, 1)
    after_stat = path.stat()
    assert path.read_bytes() == before_bytes
    assert (after_stat.st_ino, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
        before_stat.st_ctime_ns,
    )
    assert restore.set(True, expected_revision=1) == IntentRecord(True, 2)
    assert owner.read() == IntentRecord(True, 2)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("revision", [True, False, -1, 1 << 63, 0.0, "0", [], {}])
def test_cas_malformed_revision_never_changes_choice_or_file(tmp_path, durable, revision):
    path = tmp_path / "intent.json" if durable else None
    intent = ConverseIntent(path, initial_enabled=False)
    before = path.read_bytes() if path else None
    with pytest.raises((TypeError, ValueError, OverflowError)):
        intent.set(True, expected_revision=revision)
    assert intent.read() == IntentRecord(False, 0)
    if path:
        assert path.read_bytes() == before


@pytest.mark.parametrize("durable", [False, True])
def test_cas_explicit_none_preserves_unconditional_same_value_writers(tmp_path, durable):
    path = tmp_path / "intent.json" if durable else None
    intent = ConverseIntent(path, initial_enabled=False)
    assert intent.set(False, expected_revision=None) == IntentRecord(False, 1)
    assert intent.set(False) == IntentRecord(False, 2)
    assert intent.set(True, expected_revision=2) == IntentRecord(True, 3)


def test_cas_maximum_expected_revision_is_a_valid_mismatch_and_overflow_never_writes(tmp_path):
    from services.converse import intent as module

    path = tmp_path / "intent.json"
    intent = ConverseIntent(path, initial_enabled=False)
    before = path.read_bytes()
    with pytest.raises(ValueError) as caught:
        intent.set(True, expected_revision=MAX_REVISION)
    assert isinstance(caught.value, module.OwnerIntentConflict)
    assert caught.value.observed == IntentRecord(False, 0)
    assert path.read_bytes() == before
    raw = json.dumps({"enabled": False, "revision": MAX_REVISION}).encode()
    write_raw(path, raw)
    with pytest.raises(OverflowError):
        intent.set(True, expected_revision=MAX_REVISION)
    assert path.read_bytes() == raw
    assert intent.read() == IntentRecord(False, MAX_REVISION)


@pytest.mark.parametrize("leaf", ["record", "lock"])
@pytest.mark.parametrize("kind", ["permissions", "symlink", "hardlink", "fifo"])
def test_cas_mismatch_does_not_bypass_private_record_or_lock_guards(tmp_path, leaf, kind):
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
    with pytest.raises((OSError, ValueError)) as caught:
        intent.set(False, expected_revision=99)
    assert type(caught.value).__name__ != "OwnerIntentConflict"
    after = target.lstat()
    assert (before.st_ino, before.st_mode) == (after.st_ino, after.st_mode)
    assert victim.read_text() == "unrelated synthetic data"


def test_cas_comparison_waits_for_the_shared_flock_and_releases_after_conflict(tmp_path):
    path = tmp_path / "intent.json"
    intent = ConverseIntent(path)
    before = path.read_bytes()
    with (tmp_path / "intent.json.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            intent.set(False, expected_revision=99)
        assert path.read_bytes() == before
    with pytest.raises(ValueError):
        intent.set(False, expected_revision=99)
    assert intent.set(False, expected_revision=0) == IntentRecord(False, 1)


def test_cas_observes_a_real_bounded_independent_native_writer(tmp_path):
    from services.converse import intent as module

    path = tmp_path / "intent.json"
    intent = ConverseIntent(path)
    snapshot = intent.read()
    child = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from pathlib import Path; import json,sys; "
            "from services.converse.intent import ConverseIntent; "
            "r=ConverseIntent(Path(sys.argv[1])).set(False); "
            "print(json.dumps(r.__dict__))",
            str(path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert json.loads(child.stdout) == {"enabled": False, "revision": 1}
    before = path.read_bytes()
    with pytest.raises(ValueError) as caught:
        intent.set(True, expected_revision=snapshot.revision)
    assert isinstance(caught.value, module.OwnerIntentConflict)
    assert caught.value.observed == IntentRecord(False, 1)
    assert path.read_bytes() == before
    assert intent.set(True, expected_revision=1) == IntentRecord(True, 2)
