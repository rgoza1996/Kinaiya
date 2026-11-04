"""One-command local startup supervisor for host services, matching web UI, and browser."""

from __future__ import annotations

import http.client
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.distribution.installation import (
    InstallError,
    Layout,
    atomic_json,
    read_json,
)
from services.distribution.web import web_bundle_dir

RUNNING_STATE_FILENAME = "running-start.json"
DEFAULT_PORTS: dict[str, int] = {
    "session": 8760,
    "agent": 8761,
    "converse": 8762,
    "decisions": 8763,
}


def _validate_port(port: Any, label: str) -> int:
    if type(port) is not int or not (1 <= port <= 65535):
        raise InstallError(f"{label} port must be an integer between 1 and 65535")
    return port


def _service_port(config: Mapping[str, Any], service: str, default_port: int) -> int:
    port_key = f"{service}_port"
    if port_key in config and config[port_key] is not None:
        return _validate_port(config[port_key], service)
    url_key = f"{service}_url"
    raw_url = config.get(url_key)
    if isinstance(raw_url, str) and raw_url.strip():
        parsed = urlsplit(raw_url.strip())
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise InstallError(f"{service}_url must use a loopback host (127.0.0.1 or localhost)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise InstallError(f"{service}_url must not contain credentials, query, or fragment")
        if parsed.port is not None:
            return _validate_port(parsed.port, service)
    return default_port


def required_service_ports(config: Mapping[str, Any], *, web_port: int = 8780) -> dict[str, int]:
    """Return the required loopback ports for the configured local host and web UI."""
    validated_web_port = _validate_port(web_port, "web")
    ports: dict[str, int] = {
        "session": _service_port(config, "session", DEFAULT_PORTS["session"]),
        "agent": _service_port(config, "agent", DEFAULT_PORTS["agent"]),
    }
    converse_enabled = (
        isinstance(config.get("distribution"), Mapping)
        and config["distribution"].get("converse") is True
    ) or config.get("converse_enabled") is True
    if converse_enabled:
        ports["converse"] = _service_port(config, "converse", DEFAULT_PORTS["converse"])
    ports["web"] = validated_web_port
    if len(set(ports.values())) != len(ports):
        raise InstallError("Configured local service ports and web UI port must be distinct")
    return ports


def _port_listening(port: int, *, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _probe_health(
    service: str,
    port: int,
    *,
    require_identity: bool = False,
    timeout: float = 0.4,
) -> bool:
    """Probe loopback HTTP readiness without sending or logging any credentials."""
    paths = ("/health", "/") if service == "web" else ("/health",)
    for path in paths:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read(16384)
            if response.status != 200:
                continue
            if path == "/health":
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    if service == "web" and not require_identity:
                        return True
                    return False
                if not isinstance(payload, Mapping):
                    return False
                reported = payload.get("service")
                if require_identity:
                    return reported == service
                return reported in {None, service}
            if service == "web" and not require_identity:
                return True
        except (OSError, http.client.HTTPException):
            continue
        finally:
            conn.close()
    return False


def _pid_alive(pid: Any) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _inspect_running_state(layout: Layout, ports: Mapping[str, int]) -> dict[str, Any] | None:
    """Return active running state if this layout already owns live healthy services."""
    state_path = layout.data / RUNNING_STATE_FILENAME
    if not os.path.lexists(state_path):
        return None
    if state_path.is_symlink():
        raise InstallError("Running state file cannot be a symlink")
    try:
        state = read_json(state_path)
    except InstallError:
        return None
    if not isinstance(state, dict):
        return None
    if state.get("install") != str(layout.install) or state.get("data") != str(layout.data):
        return None
    recorded_ports = state.get("ports")
    if not isinstance(recorded_ports, Mapping):
        return None
    child_pids = state.get("child_pids")
    pids: list[Any] = [state.get("supervisor_pid")]
    if isinstance(child_pids, Mapping):
        pids.extend(child_pids.values())
    any_alive = any(_pid_alive(pid) for pid in pids)
    if not any_alive:
        state_path.unlink(missing_ok=True)
        return None
    if dict(recorded_ports) != dict(ports):
        return None
    if all(_probe_health(service, port, require_identity=True) for service, port in ports.items()):
        return state
    return None


def validate_safe_loopback_url(url: str) -> str:
    """Ensure the URL opened or printed is a credential-free loopback URL."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise InstallError("Refusing non-loopback or parameterized startup URL")
    return f"http://{parsed.hostname}:{parsed.port}"


def open_loopback_browser(
    url: str,
    *,
    env: Mapping[str, str] | None = None,
    opener: Callable[[str], bool] | None = None,
) -> bool:
    """Open the loopback web UI in a desktop browser when available; never fail if headless."""
    safe_url = validate_safe_loopback_url(url)
    environ = os.environ if env is None else env
    if opener is None:
        if sys.platform.startswith("linux") and not (
            environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY") or environ.get("BROWSER")
        ):
            return False

        def _default_opener(target: str) -> bool:
            return bool(webbrowser.open(target, new=2))

        opener = _default_opener
    try:
        return bool(opener(safe_url))
    except Exception:
        return False


def build_start_commands(
    layout: Layout,
    config: Mapping[str, Any],
    *,
    web_port: int = 8780,
) -> dict[str, list[str]]:
    """Build credential-free child command lines for the local host and web server."""
    _validate_port(web_port, "web")
    if (layout.bin / "kinaiya").is_file() and (layout.install / "current/launch").is_file():
        prefix = [str(layout.bin / "kinaiya")]
    else:
        prefix = [
            sys.executable,
            "-m",
            "services.launcher",
            "--data-dir",
            str(layout.data),
            "--config",
            str(layout.config),
            "--workspace",
            str(layout.workspace),
        ]
    commands = {
        "host": [*prefix, "serve", "host"],
        "web": [*prefix, "web", "--port", str(web_port)],
    }
    _assert_no_secrets_in_commands(commands, config)
    return commands


def _assert_no_secrets_in_commands(
    commands: Mapping[str, Sequence[str]], config: Mapping[str, Any]
) -> None:
    forbidden_substrings = ("Bearer ", "owner-login", "service-tokens")
    for name, argv in commands.items():
        for arg in argv:
            if any(token in str(arg) for token in forbidden_substrings):
                raise InstallError(f"Refusing secret material in '{name}' command arguments")


def _stop_owned_children(owned_children: Mapping[str, Any]) -> None:
    """Cleanly stop only child processes spawned by this supervisor."""
    for child in owned_children.values():
        try:
            if child.poll() is None:
                if hasattr(child, "send_signal"):
                    child.send_signal(signal.SIGINT)
                elif hasattr(child, "terminate"):
                    child.terminate()
        except OSError:
            pass

    for child in owned_children.values():
        try:
            if child.poll() is None:
                child.wait(timeout=3)
        except Exception:
            try:
                if hasattr(child, "terminate") and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=2)
            except Exception:
                try:
                    if hasattr(child, "kill") and child.poll() is None:
                        child.kill()
                except Exception:
                    pass


def _emit_status(stream: Any, payload: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        stream.write(json.dumps(payload) + "\n")
    else:
        url = payload["web_url"]
        if payload.get("already_running"):
            stream.write(f"Kinaiya is already running at {url}\n")
        else:
            stream.write(f"Kinaiya is running at {url}\n")
            if not payload.get("browser_opened"):
                stream.write(f"Open {url} in your browser to access the local owner UI.\n")
    stream.flush()


def start_local(
    layout: Layout,
    *,
    no_browser: bool = False,
    json_output: bool = False,
    web_port: int = 8780,
    timeout: float = 15.0,
    popen: Callable[..., Any] = subprocess.Popen,
    commands: Mapping[str, Sequence[str]] | None = None,
    browser_opener: Callable[[str], bool] | None = None,
    env: Mapping[str, str] | None = None,
    stream: Any = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
    hold: bool = True,
    on_ready: Callable[[dict[str, Any]], None] | None = None,
    fallback_source: Path | None = None,
) -> dict[str, Any]:
    """Start the configured local host, matching web UI, and optional browser cleanly."""
    if stream is None:
        stream = sys.stdout
    layout.validate()
    _validate_port(web_port, "web")
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise InstallError("Startup timeout must be a finite positive number of seconds")

    if not os.path.lexists(layout.config):
        raise InstallError(f"Missing configuration at {layout.config}. Run 'kinaiya setup' first.")
    if layout.config.is_symlink():
        raise InstallError("Configuration file cannot be a symlink")

    config = read_json(layout.config)
    if not isinstance(config, Mapping):
        raise InstallError("Configuration file must contain a JSON object")

    # Verify matching-release Flutter web assets before spawning any processes.
    web_bundle_dir(layout, fallback_source=fallback_source)

    ports = required_service_ports(config, web_port=web_port)
    web_url = validate_safe_loopback_url(f"http://127.0.0.1:{web_port}")

    # Distinguish already-running Kinaiya-owned processes from unrelated occupants.
    existing = _inspect_running_state(layout, ports)
    if existing is not None:
        payload: dict[str, Any] = {
            "status": "already_running",
            "already_running": True,
            "web_url": web_url,
            "browser_opened": False,
            "ports": dict(ports),
            "services": list(ports.keys()),
            "converse_enabled": "converse" in ports,
            "provider_connection": "not_checked",
            "task_submitted": False,
            "microphone_armed": False,
            "voice_models_loaded": False,
        }
        _emit_status(stream, payload, json_output=json_output)
        if on_ready is not None:
            on_ready(payload)
        return payload

    for service_name, port in ports.items():
        if _port_listening(port):
            raise InstallError(
                f"Port conflict on 127.0.0.1:{port} ({service_name}): an unrelated process is "
                "already listening on this port. Stop the conflicting process or choose a "
                "different port; Kinaiya never terminates unrelated listeners."
            )

    spawn_commands = (
        {name: list(argv) for name, argv in commands.items()}
        if commands is not None
        else build_start_commands(layout, config, web_port=web_port)
    )
    _assert_no_secrets_in_commands(spawn_commands, config)

    layout.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = layout.data / RUNNING_STATE_FILENAME
    owned_children: dict[str, Any] = {}
    wrote_state = False

    in_main_thread = threading.current_thread() is threading.main_thread()
    previous_sigint = None
    previous_sigterm = None

    def _handle_stop_signal(signum, frame):
        raise KeyboardInterrupt

    if in_main_thread:
        previous_sigint = signal.getsignal(signal.SIGINT)
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _handle_stop_signal)
        signal.signal(signal.SIGTERM, _handle_stop_signal)

    try:
        for name, argv in spawn_commands.items():
            owned_children[name] = popen(list(argv))

        started_at = clock_fn()
        ready_services: set[str] = set()
        while True:
            for name, child in owned_children.items():
                exit_code = child.poll()
                if exit_code is not None:
                    raise InstallError(
                        f"Managed child process '{name}' exited early with code {exit_code} before readiness."
                    )

            ready_services = {
                service_name
                for service_name, port in ports.items()
                if _probe_health(service_name, port, require_identity=False)
            }
            if len(ready_services) == len(ports):
                break

            if clock_fn() - started_at >= timeout:
                missing = [svc for svc in ports if svc not in ready_services]
                raise InstallError(
                    f"Timed out after {timeout:.1f}s waiting for local services "
                    f"({', '.join(missing)}) to become ready."
                )
            sleep_fn(0.05)

        child_pids = {
            name: child.pid
            for name, child in owned_children.items()
            if isinstance(getattr(child, "pid", None), int)
        }
        atomic_json(
            state_path,
            {
                "supervisor_pid": os.getpid(),
                "child_pids": child_pids,
                "web_url": web_url,
                "ports": dict(ports),
                "install": str(layout.install),
                "data": str(layout.data),
                "config": str(layout.config),
            },
        )
        wrote_state = True

        browser_opened = False
        if not no_browser:
            browser_opened = open_loopback_browser(web_url, env=env, opener=browser_opener)

        payload = {
            "status": "running",
            "already_running": False,
            "web_url": web_url,
            "browser_opened": browser_opened,
            "ports": dict(ports),
            "services": list(ports.keys()),
            "converse_enabled": "converse" in ports,
            "provider_connection": "not_checked",
            "task_submitted": False,
            "microphone_armed": False,
            "voice_models_loaded": False,
        }
        _emit_status(stream, payload, json_output=json_output)
        if on_ready is not None:
            on_ready(payload)

        if hold:
            while True:
                for name, child in owned_children.items():
                    exit_code = child.poll()
                    if exit_code is not None:
                        raise InstallError(
                            f"Managed child process '{name}' exited unexpectedly with code {exit_code}."
                        )
                sleep_fn(0.1)
        return payload
    except KeyboardInterrupt:
        return {
            "status": "stopped",
            "already_running": False,
            "web_url": web_url,
            "browser_opened": False,
            "ports": dict(ports),
            "services": list(ports.keys()),
            "converse_enabled": "converse" in ports,
            "provider_connection": "not_checked",
            "task_submitted": False,
            "microphone_armed": False,
            "voice_models_loaded": False,
        }
    finally:
        if in_main_thread:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
        if wrote_state:
            state_path.unlink(missing_ok=True)
        _stop_owned_children(owned_children)
