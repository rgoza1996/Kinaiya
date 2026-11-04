"""Registered media retirement refuses an ambiguous rejected original pidfd.

Interface-first with respect to A's new process/pool implementation, which was
not read. Root's reported requirement and permitted generic baseline ownership
observers inform the native fixture; this is not wholly source-blind provenance.
Only disposable synthetic CPU processes and real kernel descriptors are used.
"""

import asyncio
import errno
import fcntl
import hashlib
import os
import select
import signal
import sys

import pytest

from packages.contracts import media
from packages.resources.media_processes import (
    MediaLaunchBinding,
    MediaProcessError,
    MediaWorkerPool,
)
from packages.resources.processes import WorkerSpec
from services.session.media_spool import MediaSpool, MediaSpoolError

_CODES = {"bad_packet", "bound", "capacity", "conflict", "expired", "unavailable"}
_WAIT = 10
_SOURCE = b"synthetic retirement guard source\0" + bytes(range(32))
_PROGRAM = r"""
import os, signal, sys
with open(sys.argv[1], 'xb') as marker:
    marker.write(b'configured code must not execute before grant')
while True:
    signal.pause()
"""


def _limits():
    return media.MediaLimits(
        v=1,
        source=media.MediaSourceLimits(
            file_bytes=8388608,
            file_duration_ms=21600000,
            upload_chunk_bytes=4194304,
            owner_spool_bytes=16777216,
            global_spool_bytes=33554432,
            unsealed_uploads_per_owner=2,
            operations_global=2,
            live_spool_bytes=536870912,
            live_buffer_target_ms=120000,
            sealed_unstarted_ms=1800000,
        ),
        decode=media.MediaDecodeLimits(
            segment_ms=60000,
            cpu_ms=120000,
            wall_ms=300000,
            address_space_bytes=805306368,
            cpu_affinity_count=1,
            children_per_operation=1,
            children_global=2,
            track_limit=8,
            frames_per_track_segment=3600,
            pixels_per_frame=16777216,
            max_edge=4096,
            frame_rate=media.MediaQ(num=60, den=1),
            probe_metadata_bytes=1048576,
            stderr_bytes=65536,
        ),
        wire=media.MediaWireLimits(
            control_bytes=65536,
            body_bytes=16777216,
            audio_packet_ms=1000,
            image_bytes=8388608,
            clip_bytes=16777216,
            clip_ms=10000,
            consumer_packets=8,
            consumer_bytes=16777216,
            total_pending_bytes=67108864,
            live_backlog_ms=2000,
        ),
    )


def _binding(index=1, **changes):
    source_id = f"{index + 100:032x}"
    values = {
        "operation_id": f"{index + 1000:032x}",
        "child_instance": f"{index:032x}",
        "source": media.MediaNativeRef(backend_id="a" * 32, receipt_id=source_id, sha256="b" * 64),
        "source_id": source_id,
        "consumer": media.MediaConsumerRef(consumer_id="c" * 32, generation=1),
        "fence": media.MediaFence(
            session=media.MediaSession(session_id="session", thread_id="thread", epoch=1),
            source_revision=1,
            generation=1,
            lease_generation=1,
        ),
        "binding_sha256": hashlib.sha256(f"native-binding-{index}".encode()).hexdigest(),
    }
    values.update(changes)
    return MediaLaunchBinding(**values)


class _NativeObservation:
    """Standard OS seams only, retaining the real subprocess and exact FD."""

    def __init__(self, sentinel):
        self.sentinel = sentinel
        self.native_spawn = asyncio.create_subprocess_exec
        self.native_pidfd_open = os.pidfd_open
        self.native_close = os.close
        self.processes = []
        self.target = None
        self.readable_after_exit = False
        self.closes = []
        self.sentinel_fd = None
        info = sentinel.stat()
        self.sentinel_identity = (info.st_dev, info.st_ino)

    async def spawn(self, *args, **kwargs):
        process = await self.native_spawn(*args, **kwargs)
        self.processes.append(process)
        return process

    def pidfd_open(self, pid, flags=0):
        fd = self.native_pidfd_open(pid, flags)
        if self.target is None and any(process.pid == pid for process in self.processes):
            self.target = (pid, fd)
            # This is the newly opened stable handle for the actual captured
            # disposable child. Kill it before returning to post-open checks.
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            readable, _, _ = select.select([fd], [], [], _WAIT)
            assert readable == [fd], "actual original pidfd never reported process exit"
            self.readable_after_exit = True
        return fd

    def close(self, fd):
        if self.target is None or fd != self.target[1]:
            return self.native_close(fd)
        self.closes.append(fd)
        if len(self.closes) != 1:
            # A blind later close really destroys the sentinel, so the oracle
            # detects it; it is not replaced with a fabricated successful close.
            return self.native_close(fd)
        self.native_close(fd)
        temporary = os.open(self.sentinel, os.O_RDONLY | os.O_CLOEXEC)
        if temporary == fd:
            reused = temporary
        else:
            reused = fcntl.fcntl(temporary, fcntl.F_DUPFD_CLOEXEC, fd)
            self.native_close(temporary)
        self.sentinel_fd = reused
        assert reused == fd, "exact rejected pidfd integer was not available for reuse"
        raise OSError(errno.EIO, "untrusted-pidfd-close-detail")

    def assert_sentinel(self):
        assert self.target is not None and self.sentinel_fd == self.target[1]
        assert self.closes == [self.target[1]], "original rejected FD was closed more than once"
        info = os.fstat(self.sentinel_fd)
        assert (info.st_dev, info.st_ino) == self.sentinel_identity
        assert os.pread(self.sentinel_fd, 128, 0) == b"synthetic sentinel remains owned"

    async def reap_actual(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), _WAIT)
            assert process.returncode is not None

    def close_sentinel(self):
        if self.sentinel_fd is None:
            return
        try:
            info = os.fstat(self.sentinel_fd)
            if (info.st_dev, info.st_ino) == self.sentinel_identity:
                self.native_close(self.sentinel_fd)
        except OSError as failure:
            if failure.errno != errno.EBADF:
                raise


async def test_actual_pregrant_rejection_keeps_ambiguous_pidfd_original_charged(
    tmp_path, monkeypatch
):
    marker = tmp_path / "configured-code-marker"
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"synthetic sentinel remains owned")
    observer = _NativeObservation(sentinel)
    binding = _binding()
    pool = MediaWorkerPool(
        WorkerSpec(
            id="rejected-original",
            argv=(sys.executable, "-B", "-c", _PROGRAM, str(marker)),
            cwd=tmp_path,
            startup_timeout_s=2,
            stop_timeout_s=0.1,
        ),
        _limits(),
        current=lambda original: True,
        retire_dependencies=_positive_test_dependencies,
    )
    spool_root = tmp_path / "spool"
    spool_root.mkdir(mode=0o700)
    spool = MediaSpool(spool_root, _limits())
    upload = spool.reserve(binding.source_id, len(_SOURCE))
    digest = hashlib.sha256(_SOURCE).hexdigest()
    upload.append(0, 0, _SOURCE, digest)
    upload.seal(len(_SOURCE), digest)
    launch = pool.register(binding)
    grant = upload._open_child_grant(launch)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "create_subprocess_exec", observer.spawn)
            patch.setattr(os, "pidfd_open", observer.pidfd_open)
            patch.setattr(os, "close", observer.close)
            with pytest.raises(MediaProcessError) as refused:
                await asyncio.wait_for(pool.start(launch, grant), _WAIT)
            assert refused.value.code in _CODES
            assert "untrusted-pidfd-close-detail" not in str(refused.value)
            assert len(observer.processes) == 1
            process = observer.processes[0]
            assert observer.target is not None and observer.target[0] == process.pid
            assert observer.readable_after_exit, "post-open rejection fixture was not exercised"
            assert not marker.exists(), "configured code escaped the original pre-grant refusal"
            assert observer.closes == [observer.target[1]], "ambiguous close seam was not exercised"
            await asyncio.wait_for(process.wait(), _WAIT)
            assert process.returncode is not None

            pool.fence(launch)
            assert (await pool.settle(launch)).state == "unknown"
            assert pool.register(_binding()) is launch
            with pytest.raises(MediaProcessError) as capacity:
                pool.register(_binding(9, operation_id=binding.operation_id))
            assert capacity.value.code == "capacity"
            assert upload.delete() is False and not upload.deleted
            assert spool.reserved_bytes == len(_SOURCE) and spool.active_uploads == 1
            observer.assert_sentinel()

            for _ in range(2):
                assert (await pool.settle(launch)).state == "unknown"
                with pytest.raises(MediaProcessError):
                    await asyncio.wait_for(pool.start(launch, grant), _WAIT)
                assert len(observer.processes) == 1
                assert spool.reserved_bytes == len(_SOURCE) and not upload.deleted
                observer.assert_sentinel()
            assert (await pool.close()).state == "unknown"
            observer.assert_sentinel()
            assert spool.reserved_bytes == len(_SOURCE) and not upload.deleted
    finally:
        # Restored native close is used only for the test-owned sentinel.
        # Actual child wait is required even on assertion failure. Unknown
        # original pool/spool claims are never described as positively retired.
        try:
            pool.fence(launch)
            await pool.close()
        finally:
            try:
                await observer.reap_actual()
            finally:
                observer.close_sentinel()
                try:
                    spool.close()
                except MediaSpoolError as failure:
                    assert failure.code in _CODES


async def _positive_test_dependencies(binding, child):
    # This configured code never receives its grant. True here says nothing
    # about production inner/init/descendant or publication implementations.
    return True
