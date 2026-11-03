"""Independent synthetic oracles for the selected original-FD spool API.

Local progress/read/delete observations are not Session ACKs, permission,
publication settlement, or original decoder/provider/worker cleanup proofs.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys

import pytest

from packages.contracts import media
from services.session.media_spool import MediaSpool, MediaSpoolError

_ERROR_CODES = {"bad_packet", "bound", "capacity", "conflict", "unavailable", "expired"}
_SOURCE_A = "a" * 32
_SOURCE_B = "b" * 32


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _limits(**source_changes):
    source = {
        "file_bytes": 8388608,
        "file_duration_ms": 21600000,
        "upload_chunk_bytes": 4194304,
        "owner_spool_bytes": 16777216,
        "global_spool_bytes": 33554432,
        "unsealed_uploads_per_owner": 2,
        "operations_global": 2,
        "live_spool_bytes": 536870912,
        "live_buffer_target_ms": 120000,
        "sealed_unstarted_ms": 1800000,
    }
    source.update(source_changes)
    raw = {
        "v": 1,
        "source": source,
        "decode": {
            "segment_ms": 60000,
            "cpu_ms": 120000,
            "wall_ms": 300000,
            "address_space_bytes": 805306368,
            "cpu_affinity_count": 1,
            "children_per_operation": 1,
            "children_global": 2,
            "track_limit": 8,
            "frames_per_track_segment": 3600,
            "pixels_per_frame": 16777216,
            "max_edge": 4096,
            "frame_rate": {"num": 60, "den": 1},
            "probe_metadata_bytes": 1048576,
            "stderr_bytes": 65536,
        },
        "wire": {
            "control_bytes": 65536,
            "body_bytes": 16777216,
            "audio_packet_ms": 1000,
            "image_bytes": 8388608,
            "clip_bytes": 16777216,
            "clip_ms": 10000,
            "consumer_packets": 8,
            "consumer_bytes": 16777216,
            "total_pending_bytes": 67108864,
            "live_backlog_ms": 2000,
        },
    }
    return media.media_json(_json(raw), media.MediaLimits)


@pytest.fixture
def private_root(tmp_path):
    root = tmp_path / "spool"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _refused(operation, code=None):
    with pytest.raises(MediaSpoolError) as failure:
        operation()
    assert isinstance(failure.value, ValueError)
    assert failure.value.code in _ERROR_CODES
    if code is not None:
        assert failure.value.code == code
    assert "synthetic-untrusted-detail" not in str(failure.value)


def _attempt_local_close(operation):
    # Cleanup attempts after an assertion/refusal are never settlement evidence.
    try:
        operation()
    except MediaSpoolError as failure:
        assert failure.code in _ERROR_CODES


def _progress(value):
    return tuple(
        getattr(value, name)
        for name in ("index", "offset", "body_bytes", "body_sha256", "next_offset")
    )


def _append(upload, index, offset, body):
    return upload.append(index, offset, body, _sha(body))


def _read_all(reader):
    chunks = []
    while chunk := reader.read(65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _native_regular_files(root):
    # Test-owned synthetic state only; no production storage name/format assumed.
    return sorted(p for p in root.rglob("*") if stat.S_ISREG(p.lstat().st_mode))


def _assert_child_rejects(root, limits):
    # Isolate FIFO/blocking-open regressions and failed-constructor ownership.
    script = """
import json, sys
from pathlib import Path
from packages.contracts import media
from services.session.media_spool import MediaSpool, MediaSpoolError
limits = media.media_json(sys.argv[2].encode(), media.MediaLimits)
try:
    spool = MediaSpool(Path(sys.argv[1]), limits)
except MediaSpoolError as failure:
    print(json.dumps({"code": failure.code}))
else:
    spool.close()
    raise SystemExit("unsafe native state was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(root), media.media_bytes(limits).decode()],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["code"] in _ERROR_CODES


def test_contiguous_progress_exact_replay_and_whole_source_hash(private_root):
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, 6)
    try:
        first = _append(upload, 0, 0, b"abc")
        assert _progress(first) == (0, 0, 3, _sha(b"abc"), 3)
        assert upload.source_id == _SOURCE_A and upload.reserved_bytes == 6
        assert upload.next_offset == 3 and upload.sealed is False
        native_files = _native_regular_files(private_root)
        assert native_files
        for path in native_files:
            observed = path.stat()
            assert observed.st_uid == os.geteuid()
            assert observed.st_nlink == 1 and observed.st_mode & 0o077 == 0
        assert _progress(_append(upload, 0, 0, b"abc")) == _progress(first)
        assert upload.next_offset == 3 and spool.reserved_bytes == 6
        assert _progress(_append(upload, 1, 3, b"def")) == (1, 3, 3, _sha(b"def"), 6)
        assert upload.seal(6, _sha(b"abcdef")) is None
        assert upload.sealed is True
        reader = upload.open_reader()
        try:
            assert _read_all(reader) == b"abcdef"
        finally:
            _attempt_local_close(reader.close)
        assert upload.delete() is True
        assert upload.deleted is True and spool.reserved_bytes == 0
    finally:
        _attempt_local_close(upload.close)
        _attempt_local_close(spool.close)


@pytest.mark.parametrize(
    "index,offset,body,digest",
    [
        (0, 0, b"abd", _sha(b"abd")),
        (2, 3, b"def", _sha(b"def")),
        (1, 4, b"def", _sha(b"def")),
        (1, 3, b"def", _sha(b"different")),
        (True, 3, b"def", _sha(b"def")),
        (1, True, b"def", _sha(b"def")),
        (1, 3, bytearray(b"def"), _sha(b"def")),
        (1, 3, memoryview(b"def"), _sha(b"def")),
        (1, 3, b"", _sha(b"")),
    ],
)
def test_known_input_refusal_does_not_change_acknowledged_prefix(
    private_root, index, offset, body, digest
):
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, 6)
    try:
        original = _append(upload, 0, 0, b"abc")
        _refused(lambda: upload.append(index, offset, body, digest))
        assert upload.next_offset == 3 and spool.reserved_bytes == 6
        assert _progress(_append(upload, 0, 0, b"abc")) == _progress(original)
        _append(upload, 1, 3, b"def")
        upload.seal(6, _sha(b"abcdef"))
        reader = upload.open_reader()
        try:
            assert _read_all(reader) == b"abcdef"
        finally:
            _attempt_local_close(reader.close)
        assert upload.delete() is True
    finally:
        _attempt_local_close(upload.close)
        _attempt_local_close(spool.close)


def test_progress_and_original_upload_properties_are_read_only(private_root):
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, 3)
    try:
        progress = _append(upload, 0, 0, b"abc")
        for obj, attribute, changed in (
            (progress, "next_offset", 999),
            (progress, "body_sha256", "f" * 64),
            (upload, "next_offset", 999),
            (upload, "reserved_bytes", 999),
            (upload, "source_id", _SOURCE_B),
            (upload, "deleted", True),
        ):
            with pytest.raises((AttributeError, TypeError, ValueError)):
                setattr(obj, attribute, changed)
        assert _progress(progress) == (0, 0, 3, _sha(b"abc"), 3)
        assert upload.next_offset == 3 and upload.reserved_bytes == 3
        assert upload.delete() is True
    finally:
        _attempt_local_close(upload.close)
        _attempt_local_close(spool.close)


def test_old_chunk_replay_survives_more_than_sixty_four_chunks(private_root):
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, 72)
    payload = bytes(range(72))
    try:
        proofs = [_append(upload, i, i, payload[i : i + 1]) for i in range(72)]
        original_data = [
            p for p in _native_regular_files(private_root) if p.read_bytes() == payload
        ]
        assert len(original_data) == 1
        data_file = original_data[0]
        before = data_file.stat()
        for i in (0, 1, 63, 64, 71, 0):
            assert _progress(_append(upload, i, i, payload[i : i + 1])) == _progress(proofs[i])
        after = data_file.stat()
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        assert upload.next_offset == 72
        upload.seal(72, _sha(payload))
        reader = upload.open_reader()
        try:
            assert _read_all(reader) == payload
        finally:
            _attempt_local_close(reader.close)
        assert upload.delete() is True
    finally:
        _attempt_local_close(upload.close)
        _attempt_local_close(spool.close)


def test_replay_checks_actual_original_bytes_not_just_ledger_hash(private_root):
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, 6)
    _append(upload, 0, 0, b"abc")
    _append(upload, 1, 3, b"def")
    candidates = [p for p in _native_regular_files(private_root) if p.read_bytes() == b"abcdef"]
    assert len(candidates) == 1
    with candidates[0].open("r+b") as changed:
        changed.write(b"x")
        changed.flush()
        os.fsync(changed.fileno())
    _refused(lambda: _append(upload, 0, 0, b"abc"))
    assert upload.next_offset == 6 and spool.reserved_bytes == 6
    assert candidates[0].read_bytes() == b"xbcdef"
    _attempt_local_close(upload.close)
    _attempt_local_close(spool.close)


def test_upload_chunk_and_remaining_reservation_bounds_are_conjunctive(private_root):
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, 4194305)
    payload = b"x" * 4194304
    try:
        _refused(lambda: _append(upload, 0, 0, payload + b"x"))
        assert upload.next_offset == 0
        assert _progress(_append(upload, 0, 0, payload))[-1] == 4194304
        _refused(lambda: _append(upload, 1, 4194304, b"yz"))
        assert upload.next_offset == 4194304
        _append(upload, 1, 4194304, b"z")
        upload.seal(4194305, _sha(payload + b"z"))
        assert upload.delete() is True
    finally:
        _attempt_local_close(upload.close)
        _attempt_local_close(spool.close)


def test_selected_smaller_chunk_policy_cannot_be_bypassed(private_root):
    spool = MediaSpool(private_root, _limits(upload_chunk_bytes=2))
    upload = spool.reserve(_SOURCE_A, 3)
    try:
        _refused(lambda: _append(upload, 0, 0, b"abc"))
        assert upload.next_offset == 0
        _append(upload, 0, 0, b"ab")
        _append(upload, 1, 2, b"c")
        upload.seal(3, _sha(b"abc"))
        assert upload.delete() is True
    finally:
        _attempt_local_close(upload.close)
        _attempt_local_close(spool.close)


def test_selected_policy_does_not_alias_mutable_caller_descendants(private_root):
    limits = _limits(upload_chunk_bytes=2, owner_spool_bytes=3, global_spool_bytes=3)
    spool = MediaSpool(private_root, limits)
    # Simulate a caller mutating its own previously supplied frozen model.
    object.__setattr__(limits.source, "upload_chunk_bytes", 4194304)
    object.__setattr__(limits.source, "owner_spool_bytes", 100)
    object.__setattr__(limits.source, "global_spool_bytes", 100)
    upload = spool.reserve(_SOURCE_A, 3)
    try:
        _refused(lambda: _append(upload, 0, 0, b"abc"))
        _refused(lambda: spool.reserve(_SOURCE_B, 1))
        assert upload.next_offset == 0 and spool.reserved_bytes == 3
        assert upload.delete() is True
    finally:
        _attempt_local_close(upload.close)
        _attempt_local_close(spool.close)


@pytest.mark.parametrize(
    "policy",
    [
        {"owner_spool_bytes": 6},
        {"global_spool_bytes": 6},
        {"unsealed_uploads_per_owner": 1},
        {"operations_global": 1},
    ],
)
def test_each_reservation_or_concurrency_limit_is_conjunctive(private_root, policy):
    spool = MediaSpool(private_root, _limits(**policy))
    first = spool.reserve(_SOURCE_A, 4)
    try:
        _refused(lambda: spool.reserve(_SOURCE_B, 3))
        assert spool.reserved_bytes == 4 and spool.active_uploads == 1
        assert first.next_offset == 0
        assert first.delete() is True
        second = spool.reserve(_SOURCE_B, 3)
        assert second.delete() is True
    finally:
        first.close()
        _attempt_local_close(spool.close)


@pytest.mark.parametrize(
    "source_id,reserved",
    [
        ("../synthetic-untrusted-detail", 1),
        ("A" * 32, 1),
        ("a" * 31, 1),
        (_SOURCE_A, 0),
        (_SOURCE_A, -1),
        (_SOURCE_A, True),
        (_SOURCE_A, 1.0),
        (_SOURCE_A, 8388609),
        (_SOURCE_A, 9007199254740992),
    ],
)
def test_reservation_rejects_invalid_identity_or_integer_without_allocation(
    private_root, source_id, reserved
):
    spool = MediaSpool(private_root, _limits())
    try:
        _refused(lambda: spool.reserve(source_id, reserved))
        assert spool.reserved_bytes == 0 and spool.active_uploads == 0
        valid = spool.reserve(_SOURCE_A, 1)
        assert valid.delete() is True
    finally:
        _attempt_local_close(spool.close)


@pytest.mark.parametrize(
    "final_bytes,digest",
    [
        (5, _sha(b"abcdef")),
        (7, _sha(b"abcdef")),
        (6, _sha(b"abcdeg")),
        (0, _sha(b"")),
        (True, _sha(b"abcdef")),
    ],
)
def test_seal_requires_exact_nonempty_original_prefix_and_whole_hash(
    private_root, final_bytes, digest
):
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, 6)
    _append(upload, 0, 0, b"abc")
    _append(upload, 1, 3, b"def")
    _refused(lambda: upload.seal(final_bytes, digest))
    assert upload.sealed is False and spool.reserved_bytes == 6
    _refused(upload.open_reader)
    _attempt_local_close(upload.close)
    _attempt_local_close(spool.close)


def test_original_readers_are_bounded_read_only_and_independent(private_root):
    payload = bytes(range(256)) * 257
    spool = MediaSpool(private_root, _limits())
    upload = spool.reserve(_SOURCE_A, len(payload))
    _refused(upload.open_reader)
    _append(upload, 0, 0, payload)
    upload.seal(len(payload), _sha(payload))
    first = upload.open_reader()
    second = upload.open_reader()
    try:
        for reader in (first, second):
            for forbidden in ("write", "fileno", "dup", "path"):
                assert not hasattr(reader, forbidden)
        assert first.read(65536) == payload[:65536]
        assert second.read(1) == payload[:1]
        for invalid in (0, -1, 65537, True, "1"):
            _refused(lambda invalid=invalid: second.read(invalid))
        assert second.read(1) == payload[1:2]
        assert first.read(65536) == payload[65536:]
        assert first.read(1) == b""
        _refused(lambda: _append(upload, 1, len(payload), b"x"))
    finally:
        _attempt_local_close(first.close)
        _attempt_local_close(second.close)
        upload.delete()
        _attempt_local_close(spool.close)


def test_delete_pending_holds_charged_capacity_until_every_original_reader_closes(private_root):
    spool = MediaSpool(private_root, _limits(owner_spool_bytes=4, global_spool_bytes=4))
    upload = spool.reserve(_SOURCE_A, 4)
    _append(upload, 0, 0, b"abcd")
    upload.seal(4, _sha(b"abcd"))
    first = upload.open_reader()
    second = upload.open_reader()
    try:
        assert upload.delete() is False
        assert upload.delete_pending is True and upload.deleted is False
        assert spool.reserved_bytes == 4 and spool.active_uploads == 1
        _refused(upload.open_reader)
        _refused(lambda: spool.reserve(_SOURCE_B, 1))
        first.close()
        assert upload.deleted is False and spool.reserved_bytes == 4
        assert upload.delete() is False
        second.close()
        assert upload.deleted is True and spool.reserved_bytes == 0
        assert spool.active_uploads == 0
        assert upload.delete() is True
        _refused(lambda: spool.reserve(_SOURCE_A, 1))
        later = spool.reserve(_SOURCE_B, 1)
        assert later.delete() is True
    finally:
        _attempt_local_close(first.close)
        _attempt_local_close(second.close)
        _attempt_local_close(spool.close)


def test_delete_keeps_content_free_tombstone_and_never_reuses_source_id(private_root):
    payload = b"synthetic-payload-not-tombstone-content-123456789"
    limits = _limits()
    spool = MediaSpool(private_root, limits)
    upload = spool.reserve(_SOURCE_A, len(payload))
    _append(upload, 0, 0, payload)
    assert upload.delete() is True and spool.reserved_bytes == 0
    assert upload.delete() is True
    assert all(payload not in p.read_bytes() for p in _native_regular_files(private_root))
    assert spool.close() is True
    restarted = MediaSpool(private_root, limits)
    try:
        assert restarted.reserved_bytes == 0 and restarted.active_uploads == 0
        _refused(lambda: restarted.reserve(_SOURCE_A, len(payload)))
        later = restarted.reserve(_SOURCE_B, 1)
        assert later.delete() is True
    finally:
        _attempt_local_close(restarted.close)


def test_more_than_sixty_four_deleted_sources_do_not_evict_old_tombstone(private_root):
    limits = _limits()
    spool = MediaSpool(private_root, limits)
    for index in range(66):
        upload = spool.reserve(f"{index:032x}", 1)
        _append(upload, 0, 0, bytes([index]))
        assert upload.delete() is True
    assert spool.reserved_bytes == 0 and spool.active_uploads == 0
    assert spool.close() is True
    restarted = MediaSpool(private_root, limits)
    try:
        for index in (0, 1, 63, 64, 65):
            _refused(lambda index=index: restarted.reserve(f"{index:032x}", 1))
        assert restarted.reserved_bytes == 0 and restarted.active_uploads == 0
    finally:
        _attempt_local_close(restarted.close)


def test_ordinary_close_does_not_release_and_restart_cannot_resume_old_handle(private_root):
    limits = _limits(owner_spool_bytes=3, global_spool_bytes=3)
    spool = MediaSpool(private_root, limits)
    upload = spool.reserve(_SOURCE_A, 3)
    _append(upload, 0, 0, b"abc")
    assert upload.close() is True
    assert spool.reserved_bytes == 3 and spool.active_uploads == 1
    _refused(lambda: _append(upload, 1, 3, b"x"))
    assert spool.close() is True
    restarted = MediaSpool(private_root, limits)
    assert restarted.reserved_bytes == 3 and restarted.active_uploads == 1
    _refused(lambda: restarted.reserve(_SOURCE_A, 3))
    _refused(lambda: restarted.reserve(_SOURCE_B, 1))
    assert restarted.reserved_bytes == 3
    assert restarted.close() is False  # Unknown old originals stay charged/owned.


def test_exclusive_os_lock_prevents_borrowing_live_original_ownership(private_root):
    limits = _limits()
    first = MediaSpool(private_root, limits)
    _refused(lambda: MediaSpool(private_root, limits))
    _assert_child_rejects(private_root, limits)
    assert first.reserved_bytes == 0
    assert first.close() is True
    second = MediaSpool(private_root, limits)
    assert second.close() is True


def test_close_then_eio_never_recloses_reused_descriptor_or_refunds_unknown(tmp_path, private_root):
    # The separately selected os.close observation seam is the only fault seam.
    # Child isolation retains honest unknown state without leaking its original
    # lock/directory owners into other tests or touching pytest's descriptors.
    script = """
import errno, hashlib, json, os, sys
from pathlib import Path
from packages.contracts import media
from services.session.media_spool import MediaSpool, MediaSpoolError
codes = {"bad_packet", "bound", "capacity", "conflict", "unavailable", "expired"}
limits = media.media_json(sys.argv[2].encode(), media.MediaLimits)
spool = MediaSpool(Path(sys.argv[1]), limits)
upload = spool.reserve("a" * 32, 3)
upload.append(0, 0, b"abc", hashlib.sha256(b"abc").hexdigest())
upload.seal(3, hashlib.sha256(b"abc").hexdigest())
reader = upload.open_reader()
original_close = os.close
state = {"fd": None, "original_close_calls": 0, "blind_reclose_attempts": 0}
def ambiguous_close(fd):
    if state["fd"] is None:
        state["fd"] = fd
        state["original_close_calls"] += 1
        original_close(fd)
        sentinel = os.open(sys.argv[3], os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        if sentinel != fd:
            os.dup2(sentinel, fd)
            original_close(sentinel)
        os.write(fd, b"guard-owned-by-test")
        raise OSError(errno.EIO, "synthetic-untrusted-detail")
    if fd == state["fd"]:
        state["blind_reclose_attempts"] += 1
        raise OSError(errno.EIO, "synthetic-untrusted-detail")
    return original_close(fd)
os.close = ambiguous_close
try:
    try:
        close_result = reader.close()
    except MediaSpoolError as failure:
        assert failure.code in codes
        assert "synthetic-untrusted-detail" not in str(failure)
    else:
        assert close_result is None
    assert state["fd"] is not None, "selected OS close seam was not exercised"
    sentinel_stat = os.fstat(state["fd"])
    for operation in (reader.close, upload.close, upload.delete, spool.close):
        try:
            result = operation()
        except MediaSpoolError as failure:
            assert failure.code in codes
            assert "synthetic-untrusted-detail" not in str(failure)
        else:
            if operation != reader.close:
                assert result is False
    assert spool.reserved_bytes == 3 and spool.active_uploads == 1
    assert upload.deleted is False
    assert state["original_close_calls"] == 1
    assert state["blind_reclose_attempts"] == 0
    after = os.fstat(state["fd"])
    assert (after.st_dev, after.st_ino) == (sentinel_stat.st_dev, sentinel_stat.st_ino)
    os.lseek(state["fd"], 0, os.SEEK_SET)
    assert os.read(state["fd"], 65536) == b"guard-owned-by-test"
    print(json.dumps({"original_close_calls": 1, "blind_reclose_attempts": 0,
                      "reserved_bytes": spool.reserved_bytes, "deleted": upload.deleted}))
finally:
    os.close = original_close
    if state["fd"] is not None:
        original_close(state["fd"])  # Only the new test-owned sentinel, never original authority.
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(private_root),
            media.media_bytes(_limits()).decode(),
            str(tmp_path / "sentinel"),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "original_close_calls": 1,
        "blind_reclose_attempts": 0,
        "reserved_bytes": 3,
        "deleted": False,
    }


def test_parent_close_waits_for_actual_original_reader_and_never_refunds(private_root):
    limits = _limits(owner_spool_bytes=3, global_spool_bytes=3)
    spool = MediaSpool(private_root, limits)
    upload = spool.reserve(_SOURCE_A, 3)
    _append(upload, 0, 0, b"abc")
    upload.seal(3, _sha(b"abc"))
    reader = upload.open_reader()
    assert upload.close() is False
    assert spool.close() is False
    assert spool.reserved_bytes == 3
    _refused(lambda: MediaSpool(private_root, limits))
    _refused(lambda: spool.reserve(_SOURCE_B, 1))
    _refused(upload.open_reader)
    reader.close()
    restarted = MediaSpool(private_root, limits)
    assert restarted.reserved_bytes == 3 and restarted.active_uploads == 1
    _refused(lambda: restarted.reserve(_SOURCE_B, 1))
    assert restarted.close() is False


def test_missing_or_non_directory_root_is_rejected_without_creating_it(tmp_path):
    missing = tmp_path / "not-created"
    _refused(lambda: MediaSpool(missing, _limits()))
    assert not missing.exists()
    regular = tmp_path / "ordinary-file"
    regular.write_bytes(b"synthetic-untrusted-detail")
    regular.chmod(0o600)
    _refused(lambda: MediaSpool(regular, _limits()))
    assert regular.read_bytes() == b"synthetic-untrusted-detail"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o711, 0o770, 0o777])
def test_permissive_root_is_rejected_without_permission_repair(private_root, mode):
    private_root.chmod(mode)
    _refused(lambda: MediaSpool(private_root, _limits()))
    assert stat.S_IMODE(private_root.stat().st_mode) == mode
    assert list(private_root.iterdir()) == []


def test_final_root_symlink_is_not_followed(tmp_path, private_root):
    alias = tmp_path / "alias"
    alias.symlink_to(private_root, target_is_directory=True)
    _refused(lambda: MediaSpool(alias, _limits()))
    assert alias.is_symlink() and list(private_root.iterdir()) == []


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_each_actual_native_file_refuses_unsafe_alias_or_type(tmp_path, private_root, unsafe_kind):
    limits = _limits()
    spool = MediaSpool(private_root, limits)
    upload = spool.reserve(_SOURCE_A, 3)
    _append(upload, 0, 0, b"abc")
    upload.seal(3, _sha(b"abc"))
    assert spool.close() is True
    native_files = _native_regular_files(private_root)
    assert native_files
    for index, target in enumerate(native_files):
        backup = tmp_path / f"original-{index}"
        target.rename(backup)
        original_bytes = backup.read_bytes()
        try:
            if unsafe_kind == "symlink":
                target.symlink_to(backup)
            elif unsafe_kind == "hardlink":
                os.link(backup, target)
            else:
                os.mkfifo(target, 0o600)
            before = target.lstat()
            _assert_child_rejects(private_root, limits)
            after = target.lstat()
            assert (after.st_ino, after.st_mode, after.st_nlink) == (
                before.st_ino,
                before.st_mode,
                before.st_nlink,
            )
            assert backup.read_bytes() == original_bytes
        finally:
            target.unlink(missing_ok=True)
            backup.rename(target)


def test_malformed_native_state_is_refused_without_repair_or_deletion(private_root):
    limits = _limits()
    spool = MediaSpool(private_root, limits)
    upload = spool.reserve(_SOURCE_A, 3)
    _append(upload, 0, 0, b"abc")
    assert spool.close() is True
    originals = _native_regular_files(private_root)
    assert any(p.stat().st_size > 0 for p in originals)
    for path in originals:
        if path.stat().st_size:
            path.write_bytes(b"synthetic-untrusted-detail\xff")
    before = {p.relative_to(private_root): p.read_bytes() for p in originals}
    _assert_child_rejects(private_root, limits)
    after_paths = _native_regular_files(private_root)
    assert {p.relative_to(private_root): p.read_bytes() for p in after_paths} == before
