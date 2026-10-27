"""Sandbox isolation tests: real bubblewrap when present, fail-closed otherwise."""

from __future__ import annotations

import shutil
import socket
import threading

import pytest

from core.extensions import errors as E
from core.extensions import sandbox as S

PAYLOAD = {
    "query": "q",
    "files": [{"id": "f1", "path": "a.py", "text": "hello"}],
    "max_chars": 100,
}


def need_bwrap() -> None:
    if shutil.which("bwrap") is None or S.find_system_python() is None:
        pytest.skip("truthful skip: bubblewrap/system-python unavailable")


def write(tmp_path, name: str, body: str):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_fail_closed_without_isolation(tmp_path, monkeypatch):
    src = write(tmp_path, "s.py", "def select_context(p):\n    return []\n")
    monkeypatch.setattr(S, "BWRAP_BIN", "/nonexistent/bwrap-xyz")
    with pytest.raises(E.SandboxUnavailable):
        S.run_selector(src, PAYLOAD)


def test_echo_selector(tmp_path):
    need_bwrap()
    src = write(tmp_path, "s.py", "def select_context(p):\n    return ['f1']\n")
    assert S.run_selector(src, PAYLOAD) == ["f1"]


def test_env_cleared(tmp_path, monkeypatch):
    need_bwrap()
    monkeypatch.setenv("HX_TEST_SECRET_ABC", "s3cret")
    src = write(
        tmp_path,
        "s.py",
        "import os\ndef select_context(p):\n"
        "    return [k for k in os.environ if 'HX_TEST_SECRET' in k]\n",
    )
    assert S.run_selector(src, PAYLOAD) == []


def test_network_denied(tmp_path):
    need_bwrap()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    threading.Thread(target=lambda: srv.accept(), daemon=True).start()
    src = write(
        tmp_path,
        "s.py",
        "import socket\ndef select_context(p):\n"
        f"    s = socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
        "    s.close()\n    return ['open']\n",
    )
    with pytest.raises(E.CandidateFailure):
        S.run_selector(src, PAYLOAD, limits=S.SandboxLimits(wall_timeout_s=6.0))
    srv.close()


def test_host_files_unreadable(tmp_path):
    need_bwrap()
    secret = tmp_path / "host-secret.txt"
    secret.write_text("owner-data", encoding="utf-8")
    src = write(
        tmp_path,
        "s.py",
        f"def select_context(p):\n    open({str(secret)!r}).read()\n    return ['read']\n",
    )
    with pytest.raises(E.CandidateFailure):
        S.run_selector(src, PAYLOAD)


def test_infinite_loop_times_out(tmp_path):
    need_bwrap()
    src = write(tmp_path, "s.py", "def select_context(p):\n    while True:\n        pass\n")
    with pytest.raises(E.CandidateFailure, match="timeout"):
        S.run_selector(src, PAYLOAD, limits=S.SandboxLimits(wall_timeout_s=2.0))


def test_output_abuse_suppressed(tmp_path):
    need_bwrap()
    src = write(
        tmp_path,
        "s.py",
        "def select_context(p):\n    print('x' * (20 * 1024 * 1024))\n    return ['f1']\n",
    )
    assert S.run_selector(src, PAYLOAD) == ["f1"]


def test_memory_abuse_contained(tmp_path):
    need_bwrap()
    src = write(
        tmp_path,
        "s.py",
        "def select_context(p):\n    b = bytearray(1024 * 1024 * 1024)\n    return ['f1']\n",
    )
    with pytest.raises(E.CandidateFailure):
        S.run_selector(src, PAYLOAD, limits=S.SandboxLimits(wall_timeout_s=8.0))


def test_forged_extra_output_ignored(tmp_path):
    need_bwrap()
    src = write(
        tmp_path,
        "s.py",
        "def select_context(p):\n    return ['f1']  # no score/passed channel exists\n",
    )
    result = S.run_selector(src, PAYLOAD)
    assert result == ["f1"] and isinstance(result, list)


def test_runner_dir_holds_only_trusted_file(tmp_path):
    run_d = S.prepare_runner_dir(tmp_path / "run")
    assert sorted(p.name for p in run_d.iterdir()) == ["_runner.py"]
    assert (run_d / "_runner.py").read_bytes() == S.RUNNER_FILE.read_bytes()


def test_argv_binds_only_copies_and_tmpfs_work():
    argv = S.build_bwrap_argv("python", "/c/cand", "/c/run", "/c/in")
    assert str(S.RUNNER_FILE.parent) not in argv
    assert "/c/cand" in argv and "/c/run" in argv and "/c/in" in argv
    assert "--tmpfs" in argv and "/work" in argv
    assert "--bind" not in argv


def test_apply_limits_fails_closed_when_rlimit_denied(monkeypatch):
    import resource

    from core.extensions import _runner as R

    def _deny(*args):
        raise OSError("denied")

    monkeypatch.setattr(resource, "setrlimit", _deny)
    with pytest.raises(R._LimitsUnavailable):
        R._apply_limits(1, 256, 32)


TRUSTED_SIBLINGS = [
    "evaluator.py",
    "registry.py",
    "baseline_selector.py",
    "manifest.py",
    "payloads.py",
    "sourcecheck.py",
]


def test_runner_siblings_unreadable_even_bypassing_ast(tmp_path):
    need_bwrap()
    for name in TRUSTED_SIBLINGS:
        src = write(
            tmp_path,
            f"peek_{name.replace('.', '_')}.py",
            f"def select_context(p):\n    open('/runner/{name}').read()\n    return ['f1']\n",
        )
        with pytest.raises(E.CandidateFailure):
            S.run_selector(src, PAYLOAD)


def test_direct_fd_flood_stays_bounded(tmp_path):
    need_bwrap()
    src = write(
        tmp_path,
        "flood.py",
        "import os\ndef select_context(p):\n"
        "    blob = b'y' * 65536\n"
        "    for _ in range(300):\n"
        "        os.write(1, blob)\n"
        "        os.write(2, blob)\n"
        "    return ['f1']\n",
    )
    assert S.run_selector(src, PAYLOAD) == ["f1"]


def test_saved_fd_flood_fails_closed(tmp_path):
    need_bwrap()
    src = write(
        tmp_path,
        "sneak.py",
        "import os\ndef select_context(p):\n"
        "    blob = b'z' * 65536\n"
        "    for fd in range(3, 64):\n"
        "        try:\n"
        "            for _ in range(300):\n"
        "                os.write(fd, blob)\n"
        "        except OSError:\n"
        "            pass\n"
        "    return ['f1']\n",
    )
    with pytest.raises(E.CandidateFailure):
        S.run_selector(src, PAYLOAD, limits=S.SandboxLimits(wall_timeout_s=8.0, output_bytes=4096))


def test_cpu_limit_enforced(tmp_path):
    need_bwrap()
    import time as _t

    src = write(tmp_path, "spin.py", "def select_context(p):\n    while True:\n        pass\n")
    start = _t.monotonic()
    with pytest.raises(E.CandidateFailure):
        S.run_selector(src, PAYLOAD, limits=S.SandboxLimits(wall_timeout_s=25.0, cpu_s=1))
    assert _t.monotonic() - start < 25.0


def test_fork_cannot_escape_or_spoof(tmp_path):
    need_bwrap()
    src = write(
        tmp_path,
        "fork.py",
        "import os\ndef select_context(p):\n    os.fork()\n    return ['f1']\n",
    )
    with pytest.raises(E.CandidateFailure):
        S.run_selector(src, PAYLOAD)


@pytest.mark.parametrize(
    "operation",
    [
        "os.fork()",
        "os.execv('/usr/bin/true', ['true'])",
        "open('/tmp/flood', 'wb').write(b'x')",
        "socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
    ],
)
def test_kernel_filter_denies_process_file_and_socket_syscalls(tmp_path, operation):
    need_bwrap()
    # Deliberately bypass registration's import checks: kernel policy is
    # independently responsible for containing hostile Python introspection.
    src = write(
        tmp_path,
        "kernel.py",
        "import os, socket\ndef select_context(p):\n    " + operation + "\n    return ['f1']\n",
    )
    with pytest.raises(E.CandidateFailure, match="PermissionError"):
        S.run_selector(src, PAYLOAD)


def test_kernel_filter_unavailable_refuses_candidate_execution(tmp_path, monkeypatch):
    need_bwrap()
    runner = tmp_path / "broken_runner.py"
    runner.write_text(
        S.RUNNER_FILE.read_text().replace(
            '"libseccomp.so.2"', '"libseccomp-intentionally-unavailable.so"'
        )
    )
    monkeypatch.setattr(S, "RUNNER_FILE", runner)
    src = write(tmp_path, "pure.py", "def select_context(p):\n    return ['f1']\n")
    with pytest.raises(E.SandboxUnavailable, match="syscall isolation unavailable"):
        S.run_selector(src, PAYLOAD)
