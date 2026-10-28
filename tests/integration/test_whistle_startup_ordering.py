"""Production child startup with real seals/limits/fence and a synthetic CDLL.

No Whistle model or shared library is loaded; exact small fixture bytes replace
the native boundary only inside the owned test child, after the real fence.
"""

import ctypes.util
import platform
import sys
import tempfile
from pathlib import Path

import pytest

from packages.resources.processes import WorkerManager, WorkerSpec, _scan_group
from services.resources.whistle import WhistleEndpoint

ROOT = Path(__file__).resolve().parents[2]
DRIVER = r"""
import ctypes, errno, fcntl, hashlib, os, resource, socket, sys
from pathlib import Path
from packages.providers import whistle_backend as backend, whistle_worker as worker
weights, library = b"public synthetic model", b"public synthetic library"
mode = sys.argv[-1]
sys.argv = sys.argv[:-1]
backend.MANIFEST = {
    "weights": {"bytes": len(weights), "sha256": hashlib.sha256(weights).hexdigest()},
    "library": {"bytes": len(library), "sha256": hashlib.sha256(library).hexdigest()},
}
loaded = False

def verify():
    assert resource.getrlimit(resource.RLIMIT_FSIZE) == (0, 0)
    inputs = []
    for entry in os.listdir('/proc/self/fd'):
        try:
            if 'whistle-pinned-input' not in os.readlink('/proc/self/fd/' + entry):
                continue
        except FileNotFoundError:
            continue
        fd = int(entry)
        assert fcntl.fcntl(fd, fcntl.F_GET_SEALS) == (
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        )
        inputs.append(os.pread(fd, 128, 0))
    assert sorted(inputs) == sorted([weights, library])
    for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
        try:
            channel = socket.socket(family, socket.SOCK_STREAM)
        except OSError as exc:
            assert exc.errno == errno.EPERM
        else:
            channel.close()
            raise AssertionError('Native load occurred without the network fence')

class CDLLRefused(OSError): pass

class Function:
    def __init__(self, function): self.function = function
    def __call__(self, *args): return self.function(*args)

def load(data, count):
    global loaded
    verify()
    assert data == weights and count == len(weights)
    loaded = True
    return -1 if mode == 'load-failure' else 0

class Library:
    needle_load = Function(load)
    needle_transcribe = Function(lambda *args: (_ for _ in ()).throw(AssertionError('No inference')))

def fake_cdll(path):
    verify()
    assert path.startswith('/proc/self/fd/')
    assert Path(path).read_bytes() == library
    if mode == 'cdll-failure': raise CDLLRefused('Synthetic library refusal')
    return Library()

original_fence = worker.deny_network
def fenced():
    result = original_fence()
    # Do not replace CDLL until libc/libseccomp have installed the real fence.
    ctypes.CDLL = fake_cdll
    return result
worker.deny_network = fenced
try:
    worker.main()
except CDLLRefused:
    raise SystemExit(71)
except RuntimeError:
    if mode == 'load-failure' and loaded: raise SystemExit(72)
    raise
"""


@pytest.mark.parametrize(
    "mode,exit_code", [("ready", None), ("cdll-failure", 71), ("load-failure", 72)]
)
async def test_sealed_inputs_precede_zero_file_limit_and_native_load(tmp_path, mode, exit_code):
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or not ctypes.util.find_library("seccomp")
    ):
        pytest.skip("Optional Linux x86_64/seccomp process tier unavailable")
    directory = Path(tempfile.mkdtemp(prefix="kw85-order-", dir="/tmp"))
    endpoint = WhistleEndpoint(directory)
    workspace = tmp_path / "agent"
    workspace.mkdir()
    weights, library = tmp_path / "weights", tmp_path / "library"
    weights.write_bytes(b"public synthetic model")
    library.write_bytes(b"public synthetic library")
    script = tmp_path / "startup_fixture.py"
    script.write_text(DRIVER)
    observed = []

    async def readiness(spec, process):
        if not observed:
            observed.append(process)
        return await endpoint.probe(spec, process)

    manager = WorkerManager(
        [
            WorkerSpec(
                "whistle",
                (
                    sys.executable,
                    str(script),
                    "--socket-dir",
                    str(directory),
                    "--weights",
                    str(weights),
                    "--library",
                    str(library),
                    "--workspace",
                    str(workspace),
                    "--cpu-count",
                    "2",
                    "--memory-bytes",
                    str(1 << 30),
                    mode,
                ),
                ROOT,
                env={"PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
                startup_timeout_s=3,
                stop_timeout_s=0.1,
            )
        ],
        readiness=readiness,
    )
    try:
        result = await manager.start("whistle")
        if exit_code is None:
            assert result.get("ready"), result
            assert manager.owned_identity("whistle") is not None
        else:
            assert not result["ok"] and result["returncode"] == exit_code, result
            assert manager.owned_identity("whistle") is None
    finally:
        pid = manager.status()["workers"]["whistle"]["owned_pid"]
        assert (await manager.stop("whistle"))["ok"]
        await manager.close()
        assert observed and observed[0].returncode is not None
        assert not Path(f"/proc/{pid}").exists()
        members, scanned = _scan_group(pid)
        assert scanned and not members
        endpoint.close()
