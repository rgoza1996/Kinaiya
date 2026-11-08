"""Unit tests for Issue #395: one-command local start of host, web UI, and browser."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from services import launcher
from services.distribution import cli, start_supervisor
from services.distribution.installation import InstallError, Layout, atomic_json


def _free_ports(count: int) -> list[int]:
    sockets = []
    ports = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            ports.append(sock.getsockname()[1])
            sockets.append(sock)
    finally:
        for sock in sockets:
            sock.close()
    return ports


class _LocalFixtureChild:
    """In-process controllable child process backed by real loopback HTTP servers."""

    _next_pid = 40000

    def __init__(
        self,
        name: str,
        argv: list[str],
        service_ports: dict[str, int],
        *,
        crash_exit_code: int | None = None,
        serve_health: bool = True,
    ) -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.name = name
        self.argv = list(argv)
        self.exit_code: int | None = crash_exit_code
        self.signals_received: list[int] = []
        self.terminated = False
        self.killed = False
        self._servers: list[ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []

        if crash_exit_code is None and serve_health:
            for svc_name, port in service_ports.items():
                server = _start_health_server(svc_name, port)
                self._servers.append(server)

    def poll(self) -> int | None:
        return self.exit_code

    def send_signal(self, sig: int) -> None:
        self.signals_received.append(sig)
        self.stop(0)

    def terminate(self) -> None:
        self.terminated = True
        self.stop(0)

    def kill(self) -> None:
        self.killed = True
        self.stop(-9)

    def wait(self, timeout: float | None = None) -> int:
        if self.exit_code is None:
            self.stop(0)
        return self.exit_code or 0

    def stop(self, code: int = 0) -> None:
        if self.exit_code is None:
            self.exit_code = code
        while self._servers:
            srv = self._servers.pop()
            srv.shutdown()
            srv.server_close()


def _start_health_server(
    service_name: str, port: int, *, status_code: int = 200
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health" or (service_name == "web" and self.path == "/"):
                body = json.dumps({"service": service_name, "status": "ready"}).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _prepare_fixture_layout(
    tmp_path: Path,
    *,
    converse: bool = False,
    with_web_assets: bool = True,
    with_config: bool = True,
) -> tuple[Layout, dict[str, int]]:
    layout = Layout.for_home(tmp_path)
    session_port, agent_port, converse_port, web_port = _free_ports(4)
    ports = {
        "session": session_port,
        "agent": agent_port,
        "converse": converse_port,
        "web": web_port,
    }
    for directory in (
        layout.install,
        layout.data,
        layout.config.parent,
        layout.workspace,
        layout.bin,
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    if with_web_assets:
        web_dir = layout.install / "apps/kinaiya_flutter/build/web"
        web_dir.mkdir(parents=True, exist_ok=True)
        (web_dir / "index.html").write_text(
            "<!doctype html><title>Kinaiya</title>", encoding="utf-8"
        )
        (web_dir / "main.dart.js").write_text("// compiled flutter web bundle", encoding="utf-8")

    if with_config:
        config = {
            "session_url": f"http://127.0.0.1:{session_port}",
            "agent_url": f"http://127.0.0.1:{agent_port}",
            "converse_url": f"http://127.0.0.1:{converse_port}",
            "providers": {
                "agent": {
                    "kind": "lmstudio",
                    "base_url": "http://127.0.0.1:1234",
                    "model": "local-test-model",
                    "allow_cloud": False,
                    "max_tokens": 128,
                    "reasoning_effort": "none",
                }
            },
            "distribution": {"converse": converse},
            "policy": {
                "allow_write": False,
                "allow_shell": False,
                "network": False,
                "yolo": False,
            },
        }
        atomic_json(layout.config, config)
        atomic_json(
            layout.data / "service-tokens.json",
            {
                "owner": "super-secret-owner-token-999",
                "agent": "super-secret-agent-token-888",
                "converse": "super-secret-converse-token-777",
            },
        )
    return layout, ports


def test_cli_and_launcher_parse_start_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parser = launcher.build_parser()
    args = parser.parse_args(
        ["start", "--no-browser", "--json", "--web-port", "8899", "--timeout", "9.5"]
    )
    assert args.command == "start"
    assert args.no_browser is True
    assert args.json is True
    assert args.web_port == 8899
    assert args.timeout == 9.5

    captured: dict[str, Any] = {}
    layout = Layout.for_home(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)
    monkeypatch.setattr(
        start_supervisor,
        "start_local",
        lambda chosen, **kwargs: (
            captured.update({"layout": chosen, **kwargs}) or {"status": "running"}
        ),
    )
    res = cli.dispatch(args)
    assert res["status"] == "running"
    assert captured["layout"] == layout
    assert captured["no_browser"] is True
    assert captured["json_output"] is True
    assert captured["web_port"] == 8899
    assert captured["timeout"] == 9.5


def test_start_timeout_before_readiness_terminates_owned_children(tmp_path: Path) -> None:
    layout, ports = _prepare_fixture_layout(tmp_path, converse=False)
    spawned: list[_LocalFixtureChild] = []
    now = 0.0

    def fake_clock() -> float:
        return now

    def advance_sleep(seconds: float) -> None:
        nonlocal now
        now += 0.5

    def fake_popen(argv: list[str]) -> _LocalFixtureChild:
        child = _LocalFixtureChild("unready", argv, {}, serve_health=False)
        spawned.append(child)
        return child

    with pytest.raises(InstallError, match="Timed out after 1.0s waiting for local services"):
        start_supervisor.start_local(
            layout,
            web_port=ports["web"],
            timeout=1.0,
            popen=fake_popen,
            clock_fn=fake_clock,
            sleep_fn=advance_sleep,
            stream=io.StringIO(),
            hold=False,
        )

    assert len(spawned) == 2
    assert all(child.poll() is not None for child in spawned)


def test_start_healthy_agent_only_launches_host_and_web_and_opens_browser(
    tmp_path: Path,
) -> None:
    layout, ports = _prepare_fixture_layout(tmp_path, converse=False)
    spawned: list[_LocalFixtureChild] = []
    opened_urls: list[str] = []
    out = io.StringIO()

    def fake_popen(argv: list[str]) -> _LocalFixtureChild:
        if "serve" in argv and "host" in argv:
            child = _LocalFixtureChild(
                "host", argv, {"session": ports["session"], "agent": ports["agent"]}
            )
        else:
            child = _LocalFixtureChild("web", argv, {"web": ports["web"]})
        spawned.append(child)
        return child

    result = start_supervisor.start_local(
        layout,
        web_port=ports["web"],
        timeout=3.0,
        popen=fake_popen,
        browser_opener=lambda url: opened_urls.append(url) or True,
        stream=out,
        hold=False,
    )

    assert result["status"] == "running"
    assert result["already_running"] is False
    assert result["web_url"] == f"http://127.0.0.1:{ports['web']}"
    assert result["browser_opened"] is True
    assert result["services"] == ["session", "agent", "web"]
    assert result["converse_enabled"] is False
    assert result["task_submitted"] is False
    assert result["microphone_armed"] is False
    assert result["voice_models_loaded"] is False
    assert opened_urls == [f"http://127.0.0.1:{ports['web']}"]
    assert f"http://127.0.0.1:{ports['web']}" in out.getvalue()
    # Clean exit removes running-start.json and stops owned children
    assert not (layout.data / start_supervisor.RUNNING_STATE_FILENAME).exists()
    assert len(spawned) == 2
    assert all(child.poll() is not None for child in spawned)
    # Zero secrets in stdout, URL, or child argv
    combined_text = (
        out.getvalue() + " ".join(" ".join(c.argv) for c in spawned) + " ".join(opened_urls)
    )
    assert "super-secret" not in combined_text


def test_start_headless_no_browser_and_json_modes_print_working_url_without_failing(
    tmp_path: Path,
) -> None:
    layout, ports = _prepare_fixture_layout(tmp_path, converse=True)

    def fake_popen(argv: list[str]) -> _LocalFixtureChild:
        if "serve" in argv:
            return _LocalFixtureChild(
                "host",
                argv,
                {
                    "session": ports["session"],
                    "agent": ports["agent"],
                    "converse": ports["converse"],
                },
            )
        return _LocalFixtureChild("web", argv, {"web": ports["web"]})

    # 1. Headless environment (no DISPLAY / WAYLAND_DISPLAY) with --json
    json_out = io.StringIO()
    res_json = start_supervisor.start_local(
        layout,
        json_output=True,
        web_port=ports["web"],
        timeout=3.0,
        popen=fake_popen,
        env={},
        stream=json_out,
        hold=False,
    )
    parsed = json.loads(json_out.getvalue().strip())
    assert parsed["status"] == "running"
    assert parsed["browser_opened"] is False
    assert parsed["converse_enabled"] is True
    assert parsed["web_url"] == f"http://127.0.0.1:{ports['web']}"
    assert res_json["services"] == ["session", "agent", "converse", "web"]

    # 2. Explicit --no-browser mode with human-readable output
    text_out = io.StringIO()
    opener_called = False

    def fail_if_called(url: str) -> bool:
        nonlocal opener_called
        opener_called = True
        return True

    res_no_browser = start_supervisor.start_local(
        layout,
        no_browser=True,
        web_port=ports["web"],
        timeout=3.0,
        popen=fake_popen,
        browser_opener=fail_if_called,
        stream=text_out,
        hold=False,
    )
    assert opener_called is False
    assert res_no_browser["browser_opened"] is False
    assert f"http://127.0.0.1:{ports['web']}" in text_out.getvalue()


def test_start_detects_already_running_kinaiya_instance_without_spawning_duplicates(
    tmp_path: Path,
) -> None:
    layout, ports = _prepare_fixture_layout(tmp_path, converse=False)
    servers = [
        _start_health_server("session", ports["session"]),
        _start_health_server("agent", ports["agent"]),
        _start_health_server("web", ports["web"]),
    ]
    try:
        state_file = layout.data / start_supervisor.RUNNING_STATE_FILENAME
        atomic_json(
            state_file,
            {
                "supervisor_pid": os.getpid(),
                "child_pids": {"host": os.getpid(), "web": os.getpid()},
                "web_url": f"http://127.0.0.1:{ports['web']}",
                "ports": {
                    "session": ports["session"],
                    "agent": ports["agent"],
                    "web": ports["web"],
                },
                "install": str(layout.install),
                "data": str(layout.data),
                "config": str(layout.config),
            },
        )
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

        spawn_count = 0

        def forbid_popen(argv: list[str]) -> Any:
            nonlocal spawn_count
            spawn_count += 1
            raise AssertionError("Must not spawn duplicate processes when already running")

        out = io.StringIO()
        result = start_supervisor.start_local(
            layout,
            json_output=True,
            web_port=ports["web"],
            popen=forbid_popen,
            stream=out,
            hold=False,
        )
        assert spawn_count == 0
        assert result["status"] == "already_running"
        assert result["already_running"] is True
        assert result["web_url"] == f"http://127.0.0.1:{ports['web']}"
        assert state_file.exists()
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()


def test_start_refuses_unrelated_port_conflict_and_never_kills_unrelated_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, ports = _prepare_fixture_layout(tmp_path, converse=False)
    # Occupy the agent port with an unrelated listener
    unrelated_server = _start_health_server("unrelated-app", ports["agent"])
    kill_calls: list[tuple[int, int]] = []
    real_kill = os.kill

    def spy_kill(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))
        real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", spy_kill)
    try:
        with pytest.raises(InstallError, match=rf"Port conflict on 127\.0\.0\.1:{ports['agent']}"):
            start_supervisor.start_local(
                layout,
                web_port=ports["web"],
                popen=lambda argv: pytest.fail("Must not spawn children when port is busy"),
                stream=io.StringIO(),
                hold=False,
            )
        # Verify the unrelated listener was never signaled and is still alive and serving
        assert kill_calls == []
        assert start_supervisor._port_listening(ports["agent"]) is True
    finally:
        unrelated_server.shutdown()
        unrelated_server.server_close()


def test_start_cleans_up_sibling_owned_children_on_early_child_crash(
    tmp_path: Path,
) -> None:
    layout, ports = _prepare_fixture_layout(tmp_path, converse=False)
    spawned: dict[str, _LocalFixtureChild] = {}

    def fake_popen(argv: list[str]) -> _LocalFixtureChild:
        if "serve" in argv:
            child = _LocalFixtureChild(
                "host", argv, {"session": ports["session"], "agent": ports["agent"]}
            )
            spawned["host"] = child
            return child
        # Web child crashes immediately during startup
        child = _LocalFixtureChild("web", argv, {}, crash_exit_code=2)
        spawned["web"] = child
        return child

    with pytest.raises(InstallError, match="Managed child process 'web' exited early with code 2"):
        start_supervisor.start_local(
            layout,
            web_port=ports["web"],
            timeout=2.0,
            popen=fake_popen,
            stream=io.StringIO(),
            hold=False,
        )

    assert "host" in spawned
    assert spawned["host"].poll() is not None
    assert spawned["host"].signals_received == [signal.SIGINT]
    assert start_supervisor._port_listening(ports["session"]) is False
    assert not (layout.data / start_supervisor.RUNNING_STATE_FILENAME).exists()


def test_start_missing_config_and_missing_web_assets_fail_closed_with_remediation(
    tmp_path: Path,
) -> None:
    # 1. Missing config
    layout_no_cfg, ports = _prepare_fixture_layout(
        tmp_path / "no-cfg", with_config=False, with_web_assets=True
    )
    with pytest.raises(InstallError, match="Run 'kinaiya setup' first"):
        start_supervisor.start_local(
            layout_no_cfg,
            web_port=ports["web"],
            popen=lambda argv: pytest.fail("Must not spawn without config"),
            stream=io.StringIO(),
            hold=False,
        )

    # 2. Missing Flutter web assets
    layout_no_web, ports2 = _prepare_fixture_layout(
        tmp_path / "no-web", with_config=True, with_web_assets=False
    )
    with pytest.raises(InstallError, match="flutter build web"):
        start_supervisor.start_local(
            layout_no_web,
            web_port=ports2["web"],
            popen=lambda argv: pytest.fail("Must not spawn without web bundle"),
            stream=io.StringIO(),
            hold=False,
        )


def test_start_sigint_ctrl_c_terminates_only_owned_children_and_removes_state_file(
    tmp_path: Path,
) -> None:
    layout, ports = _prepare_fixture_layout(tmp_path, converse=False)
    spawned: dict[str, _LocalFixtureChild] = {}
    state_file = layout.data / start_supervisor.RUNNING_STATE_FILENAME
    ready_seen_mode: int | None = None

    def fake_popen(argv: list[str]) -> _LocalFixtureChild:
        if "serve" in argv:
            child = _LocalFixtureChild(
                "host", argv, {"session": ports["session"], "agent": ports["agent"]}
            )
            spawned["host"] = child
            return child
        child = _LocalFixtureChild("web", argv, {"web": ports["web"]})
        spawned["web"] = child
        return child

    def on_ready(payload: dict[str, Any]) -> None:
        nonlocal ready_seen_mode
        assert state_file.is_file()
        ready_seen_mode = stat.S_IMODE(state_file.stat().st_mode)

    ticks = 0

    def interrupt_on_hold(seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ready_seen_mode is not None:
            raise KeyboardInterrupt

    result = start_supervisor.start_local(
        layout,
        no_browser=True,
        web_port=ports["web"],
        timeout=3.0,
        popen=fake_popen,
        stream=io.StringIO(),
        sleep_fn=interrupt_on_hold,
        hold=True,
        on_ready=on_ready,
    )

    assert ready_seen_mode == 0o600
    assert result["status"] == "stopped"
    assert not state_file.exists()
    assert set(spawned) == {"host", "web"}
    for child in spawned.values():
        assert child.poll() is not None
        assert child.signals_received == [signal.SIGINT]


def test_bare_kinaiya_routes_to_setup_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Issue #499: bare kinaiya routes to setup when config.json is missing."""
    layout = Layout.for_home(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda *args, **kwargs: layout)
    setup_called = False

    def fake_dispatch(args):
        nonlocal setup_called
        assert args.command == "setup"
        setup_called = True
        return {"status": "setup_completed"}

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(cli, "print_command_result", lambda args, res: None)

    launcher.main([])

    assert setup_called is True
    err = capsys.readouterr().err
    assert "No configuration found — starting setup." in err


def test_bare_kinaiya_routes_to_start_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #499: bare kinaiya routes to start when config.json exists."""
    layout, ports = _prepare_fixture_layout(tmp_path, converse=False)
    monkeypatch.setattr(cli, "chosen_layout", lambda *args, **kwargs: layout)
    start_called = False

    def fake_dispatch(args):
        nonlocal start_called
        assert args.command == "start"
        start_called = True
        return {"status": "started"}

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(cli, "print_command_result", lambda args, res: None)

    launcher.main([])

    assert start_called is True


def test_kinaiya_help_subcommand(capsys: pytest.CaptureFixture) -> None:
    """Issue #499: kinaiya help and kinaiya --help display usage cleanly and exit 0."""
    with pytest.raises(SystemExit) as exc_info:
        launcher.main(["help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "Kinaiya independent runtimes and Agent CLI" in out
    assert "usage:" in out


def test_setup_no_start_flag_bypasses_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #506: --no-start saves configuration without prompting or starting services."""
    layout = Layout.for_home(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda *args, **kwargs: layout)

    from services.distribution import wizard

    monkeypatch.setattr(wizard, "run_wizard", lambda *args, **kwargs: {"status": "wizard_done"})

    parser = launcher.build_parser()
    args = parser.parse_args(["setup", "--no-start"])
    res = cli.dispatch(args)
    assert res == {"status": "wizard_done"}
