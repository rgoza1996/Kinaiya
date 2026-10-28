"""Sealed configuration and real controller-death behavior on Linux."""

import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from packages.resources.processes import _make_config_fd


def test_configuration_memfd_is_actually_sealed():
    fd, owner = _make_config_fd(b"public synthetic config")
    try:
        assert owner is None
        with pytest.raises(OSError):
            os.write(fd, b"altered")
        with pytest.raises(OSError):
            os.ftruncate(fd, 0)
    finally:
        os.close(fd)


async def test_direct_managed_worker_dies_when_its_controller_is_killed(tmp_path):
    child_marker = tmp_path / "child-pid"
    parent_marker = tmp_path / "parent-ready"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os,time\nfrom pathlib import Path\nPath('child-pid').write_text(str(os.getpid()))\nwhile True:time.sleep(.1)\n"
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import asyncio,sys\nfrom pathlib import Path\n"
        "from packages.resources import WorkerManager,WorkerSpec\n"
        "async def probe(spec,proc):\n p=Path('child-pid');return p.exists() and p.read_text()==str(proc.pid)\n"
        "async def main():\n"
        " m=WorkerManager([WorkerSpec('child',(sys.executable,str(Path('child.py').resolve())),Path.cwd())],readiness=probe)\n"
        " r=await m.start('child')\n assert r['ok'],r\n Path('parent-ready').write_text('ready')\n await asyncio.Event().wait()\n"
        "asyncio.run(main())\n"
    )
    root = Path(__file__).resolve().parents[2]
    parent = subprocess.Popen(
        [sys.executable, str(parent_script)],
        cwd=tmp_path,
        env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_fd = None
    try:
        for _ in range(100):
            if parent_marker.exists():
                break
            assert parent.poll() is None, "Synthetic controller exited before readiness"
            await asyncio.sleep(0.02)
        assert parent_marker.exists()
        child_pid = int(child_marker.read_text())
        stat = Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()
        assert int(stat[1]) == parent.pid
        child_fd = os.pidfd_open(child_pid)
        parent.kill()
        parent.wait(timeout=2)
        for _ in range(100):
            path = Path(f"/proc/{child_pid}/stat")
            try:
                state = path.read_text().rsplit(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                # A proc entry can disappear after open but before read completes.
                state = "gone"
            if state in {"gone", "Z", "X"}:
                break
            await asyncio.sleep(0.02)
        assert state in {"gone", "Z", "X"}, "Managed worker outlived its controller"
    finally:
        if child_fd is not None:
            try:
                signal.pidfd_send_signal(child_fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(child_fd)
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
