"""#623: real bounded Dart transport against the disabled native Converse owner."""

import asyncio
import hashlib
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from flutter_project_helpers import isolated_flutter_project

from packages.providers.text import ScriptedProvider
from services.common.auth import Auth
from services.converse.api import create_app
from services.converse.capture_settings import CaptureModeSettings
from services.converse.runtime import ConverseRuntime
from services.session.store import LocalJournal, Store


def test_actual_dart_capture_mode_transport(tmp_path):
    flutter = shutil.which("flutter")
    if flutter is None:
        pytest.skip("The pinned Flutter SDK is required for real Dart/Converse interoperability")
    requests, failures = [], []
    control = {}
    ready = threading.Event()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]

    def serve():
        native = store = runtime = None
        try:
            # Match the launcher: create, use and retire the original SQLite
            # owner on the server thread. Never disable its native thread guard.
            native = CaptureModeSettings(
                lambda: "rolling_windows",
                path=tmp_path / "private/converse-capture-mode.sqlite",
                workspace=tmp_path / "workspace",
            )
            store = Store(tmp_path / "journal.sqlite")
            provider = ScriptedProvider([])
            runtime = ConverseRuntime(LocalJournal(store, "converse"), provider)
            runtime.enabled = False
            runtime.capture_settings = native
            app = create_app(runtime, Auth({"owner": "o" * 32, "agent": "a" * 32}))

            @app.middleware("http")
            async def record(request, call_next):
                response = await call_next(request)
                requests.append((request.method, request.url.path, response.status_code))
                return response

            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            control["server"] = server
            ready.set()
            server.run(sockets=[sock])
        except BaseException as error:
            failures.append(type(error).__name__)
        finally:
            ready.set()
            if runtime is not None:
                try:
                    assert runtime.enabled is False
                    assert provider.calls == [] and store.read() == []
                    assert (
                        not runtime._active and not runtime._stt_operations and not runtime._speech
                    )
                    assert native.snapshot()["revision"] == 4
                    assert native.db.execute(
                        "SELECT COUNT(*) FROM capture_mode_operations"
                    ).fetchone() == (4,)
                    asyncio.run(runtime.close())
                    assert native.closed and native._fd is None
                except Exception as error:
                    failures.append(type(error).__name__)
            for owner in (native, store):
                if owner is not None:
                    try:
                        owner.close()
                    except Exception as error:
                        failures.append(type(error).__name__)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert ready.wait(timeout=5) and not failures
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                result = httpx.get(
                    url + "/v1/converse/options/capture-mode",
                    headers={"Authorization": "Bearer " + "o" * 32},
                    timeout=0.5,
                )
                if result.status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.02)
        else:
            pytest.fail("Original temporary Converse did not become ready")
        root = Path(__file__).resolve().parents[2]
        names = (
            subprocess.check_output(
                ["git", "ls-files", "-z", "--", "apps/kinaiya_flutter"], cwd=root
            )
            .decode()
            .split("\0")
        )
        original_inputs = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names if name
        }
        project = isolated_flutter_project(root, tmp_path / "flutter-project")
        process = subprocess.run(
            [
                flutter,
                "test",
                "--reporter",
                "expanded",
                "test/capture_modes_live_test.dart",
            ],
            cwd=project,
            env={
                **os.environ,
                "KINAIYA_LIVE_CAPTURE_URL": url,
                "KINAIYA_LIVE_CAPTURE_TOKEN": "o" * 32,
            },
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert process.returncode == 0, process.stdout + process.stderr
        assert "All tests passed!" in process.stdout
        assert (
            hashlib.sha256((project / "pubspec.lock").read_bytes()).hexdigest()
            == original_inputs["apps/kinaiya_flutter/pubspec.lock"]
        ), "Actual Capture Mode Dart fixture changed its declared dependency lock"
        assert {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in original_inputs
        } == original_inputs, "Actual Capture Mode Dart fixture changed declared application source"
        allowed = {
            "/v1/converse/options/capture-mode",
            "/v1/converse/options/capture-mode-changes/lookup",
        }
        assert all(path in allowed for _, path, _ in requests)
        saves = [
            status
            for method, path, status in requests
            if method == "POST" and path == "/v1/converse/options/capture-mode"
        ]
        assert saves.count(200) == 7 and saves.count(409) == 1
        assert any(status == 403 for _, _, status in requests)
        lookups = [status for method, path, status in requests if path.endswith("/lookup")]
        assert lookups == [200] * 4
    finally:
        if "server" in control:
            control["server"].should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "Original temporary Converse thread did not retire"
        assert not failures, failures
