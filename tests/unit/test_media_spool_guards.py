"""Original-owner filesystem and cancellation regressions for media storage."""

import hashlib
import json
import subprocess
import sys

import pytest
from test_media_spool import _limits

from packages.contracts import media
from services.session.media_spool import MediaSpool, MediaSpoolError


def test_parent_directory_symlink_is_refused_before_native_file_creation(tmp_path):
    parent = tmp_path / "real-parent"
    parent.mkdir(mode=0o700)
    root = parent / "private-spool"
    root.mkdir(mode=0o700)
    alias = tmp_path / "parent-alias"
    alias.symlink_to(parent, target_is_directory=True)
    with pytest.raises(MediaSpoolError):
        MediaSpool(alias / "private-spool", _limits())
    assert alias.is_symlink()
    assert list(root.iterdir()) == []


def test_sixteen_original_readers_are_concurrent_slots_not_a_lifetime_limit(tmp_path):
    root = tmp_path / "private-spool"
    root.mkdir(mode=0o700)
    spool = MediaSpool(root, _limits())
    upload = spool.reserve("a" * 32, 3)
    digest = hashlib.sha256(b"abc").hexdigest()
    readers = []
    try:
        upload.append(0, 0, b"abc", digest)
        upload.seal(3, digest)
        readers = [upload.open_reader() for _ in range(16)]
        with pytest.raises(MediaSpoolError) as failure:
            upload.open_reader()
        assert failure.value.code == "capacity"
        assert spool.reserved_bytes == 3 and spool.active_uploads == 1
        for reader in readers:
            assert reader.read(1) == b"a"
        readers.pop().close()
        replacement = upload.open_reader()
        readers.append(replacement)
        assert replacement.read(3) == b"abc"
        assert upload.delete() is False
        assert upload.deleted is False and spool.reserved_bytes == 3
        while readers:
            readers.pop().close()
        assert upload.deleted is True and spool.reserved_bytes == 0
        assert spool.close() is True
    finally:
        for reader in readers:
            reader.close()
        upload.delete()
        spool.close()


@pytest.mark.parametrize("fault", ["lock_growth", "cancelled_original_close"])
def test_uncertain_original_storage_stays_charged_in_isolated_child(tmp_path, fault):
    root = tmp_path / "private-spool"
    root.mkdir(mode=0o700)
    # Unknown originals intentionally remain owned. A child contains those
    # descriptors; process exit supplies no native deletion/settlement receipt.
    script = """
import asyncio, hashlib, json, os, stat, sys
from pathlib import Path
from packages.contracts import media
from services.session.media_spool import MediaSpool, MediaSpoolError
root=Path(sys.argv[1])
limits=media.media_json(sys.argv[2].encode(), media.MediaLimits)
spool=MediaSpool(root, limits)
upload=spool.reserve('a'*32, 3)
digest=hashlib.sha256(b'abc').hexdigest()
upload.append(0,0,b'abc',digest)
upload.seal(3,digest)
if sys.argv[3]=='lock_growth':
    # Discover the empty native lock by actual file state, without assuming a
    # private storage name or changing unrelated user files.
    empty=[p for p in root.iterdir()
           if stat.S_ISREG(p.lstat().st_mode) and p.stat().st_size==0]
    assert len(empty)==1
    with empty[0].open('wb') as changed:
        changed.write(b'synthetic-lock-growth')
        changed.flush()
        os.fsync(changed.fileno())
    try:
        spool.reserve('b'*32,1)
    except MediaSpoolError as failure:
        assert failure.code=='unavailable'
    else:
        raise AssertionError('changed original metadata accepted')
else:
    reader=upload.open_reader()
    real_close=os.close
    calls=[]
    def interrupted_close(fd):
        calls.append(fd)
        real_close(fd)
        raise asyncio.CancelledError()
    os.close=interrupted_close
    try:
        try:
            reader.close()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('original cancellation was hidden')
    finally:
        os.close=real_close
    assert len(calls)==1
assert spool.reserved_bytes==3 and spool.active_uploads==1
assert upload.deleted is False
for operation in (upload.close, upload.delete, spool.close):
    try:
        result=operation()
    except MediaSpoolError as failure:
        assert failure.code=='unavailable'
    else:
        assert result is False
assert spool.reserved_bytes==3 and spool.active_uploads==1
assert upload.deleted is False
print(json.dumps({'reserved_bytes':3,'deleted':False,'fault':sys.argv[3]}))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            str(root),
            media.media_bytes(_limits()).decode(),
            fault,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"reserved_bytes": 3, "deleted": False, "fault": fault}
