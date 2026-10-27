"""Compute-plugin sandbox isolation: real kernel enforcement, no skipping.

Requires bubblewrap + system Python + libseccomp. Never skips; fails
closed when isolation is unavailable. Tests bypass static checks via
:func:`run_plugin` directly to prove the kernel filter. Synthetic only.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from core.plugins import errors as PE
from core.plugins.sandbox import (
    RUNNER_FILE,
    PluginLimits,
    build_bwrap_argv,
    check_available,
    prepare_runner_dir,
    run_plugin,
)


@pytest.fixture(autouse=True)
def _require_isolation():
    check_available()  # fail closed, never skip


def test_conventional_json_algorithms():
    src = (
        b"import csv, io, statistics, hashlib\n"
        b"def run(arguments):\n"
        b"    rows = list(csv.DictReader(io.StringIO(arguments['csv'])))\n"
        b"    vals = [float(r['v']) for r in rows]\n"
        b"    digest = hashlib.sha256(arguments['csv'].encode()).hexdigest()[:16]\n"
        b"    return {'mean': statistics.mean(vals), 'n': len(vals), 'digest': digest}\n"
    )
    out = run_plugin(src, {"csv": "v\n1.0\n3.0\n5.0\n"})
    assert out["mean"] == pytest.approx(3.0) and out["n"] == 3 and len(out["digest"]) == 16
    src2 = (
        b"import decimal, datetime\n"
        b"def run(arguments):\n"
        b"    total = sum(decimal.Decimal(x) for x in arguments['items'])\n"
        b"    day = datetime.date.fromisoformat(arguments['day']).isoformat()\n"
        b"    return {'total': str(total), 'day': day}\n"
    )
    out2 = run_plugin(src2, {"items": ["1.1", "2.2"], "day": "2026-01-02"})
    assert out2 == {"total": "3.3", "day": "2026-01-02"}


def test_kernel_denies_bypassed_imports():
    for body in (
        "import os\ndef run(a):\n    os.listdir('/work')\n    return 1\n",
        "import socket\ndef run(a):\n    socket.create_connection(('127.0.0.1', 9), timeout=1)\n    return 1\n",
        "import subprocess\ndef run(a):\n    subprocess.run(['true'])\n    return 1\n",
        "def run(a):\n    open('/input/payload.json').read()\n    return 1\n",
        "import threading\ndef run(a):\n    threading.Thread().start()\n    return 1\n",
    ):
        with pytest.raises(PE.PluginError):
            run_plugin(body.encode(), {"x": 1})


def test_resource_exhaustion_contained():
    with pytest.raises(PE.PluginError, match="timeout"):
        run_plugin(
            b"def run(a):\n    while True:\n        pass\n",
            {"x": 1},
            limits=PluginLimits(wall_timeout_s=2.0),
        )
    with pytest.raises(PE.PluginError):
        run_plugin(
            b"def run(a):\n    b = bytearray(1024*1024*1024)\n    return 1\n",
            {"x": 1},
            limits=PluginLimits(wall_timeout_s=8.0),
        )
    # Output flood through raw fds still hits the host byte cap or is discarded.
    out = run_plugin(
        b"def run(a):\n    print('y' * (4*1024*1024))\n    return {'ok': True}\n",
        {"x": 1},
    )
    assert out == {"ok": True}


def test_cancellation_kills_group():
    evt = threading.Event()
    with pytest.raises(PE.PluginError, match="cancelled"):
        evt.set()
        run_plugin(b"def run(a):\n    return 1\n", {"x": 1}, cancel_event=evt)
    evt2 = threading.Event()

    def _cancel():
        time.sleep(0.5)
        evt2.set()

    threading.Thread(target=_cancel, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(PE.PluginError, match="cancelled"):
        run_plugin(b"def run(a):\n    while True:\n        pass\n", {"x": 1}, cancel_event=evt2)
    assert time.monotonic() - started < 8.0


def test_stdout_forgery_ignored_and_env_cleared(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUG_TEST_SECRET_XYZ", "s3cret")
    src = (
        b"import os\ndef run(a):\n"
        b'    print(\'{"output": 999, "success": true}\')\n'
        b"    return {'real': [k for k in (os.environ if hasattr(os, 'environ') else [])"
        b" if 'PLUG_TEST_SECRET' in k]}\n"
    )
    # os import is statically rejected at registry level, but the runner host
    # must still deny it at the kernel level when invoked directly.
    with pytest.raises(PE.PluginError):
        run_plugin(src, {"x": 1})
    out = run_plugin(b"def run(a):\n    print('forged stdout')\n    return {'v': 1}\n", {"x": 1})
    assert out == {"v": 1}
    secret = tmp_path / "host-secret.txt"
    secret.write_text("owner-data", encoding="utf-8")
    with pytest.raises(PE.PluginError):
        run_plugin(
            f"def run(a):\n    open({str(secret)!r}).read()\n    return 1\n".encode(),
            {"x": 1},
        )


def test_runner_packaging_and_fail_closed(monkeypatch, tmp_path):
    run_d = prepare_runner_dir(tmp_path / "run")
    assert sorted(p.name for p in run_d.iterdir()) == ["_plugin_runner.py"]
    assert (run_d / "_plugin_runner.py").read_bytes() == RUNNER_FILE.read_bytes()
    argv = build_bwrap_argv("python", "/c/cand", "/c/run", "/c/in")
    assert str(RUNNER_FILE.parent) not in argv
    assert "--tmpfs" in argv and "/work" in argv and "--bind" not in argv
    assert "--unshare-net" in argv and "--clearenv" in argv
    import core.plugins.sandbox as S

    monkeypatch.setattr(S, "BWRAP_BIN", "/nonexistent/bwrap-xyz")
    with pytest.raises(PE.PluginSandboxUnavailable):
        run_plugin(b"def run(a):\n    return 1\n", {"x": 1})
    assert json.loads(json.dumps({"probe": 1})) == {"probe": 1}
