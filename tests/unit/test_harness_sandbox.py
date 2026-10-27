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
        "def select_context(p):\n"
        f"    open({str(secret)!r}).read()\n    return ['read']\n",
    )
    with pytest.raises(E.CandidateFailure):
        S.run_selector(src, PAYLOAD)


def test_infinite_loop_times_out(tmp_path):
    need_bwrap()
    src = write(
        tmp_path, "s.py", "def select_context(p):\n    while True:\n        pass\n"
    )
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
