"""Exercise the real Linux installer lifecycle on the current host (#261).

Run from a clean source checkout on a disposable native Linux host or CI runner:

    python -m scripts.qualify_linux_install --root "$RUNNER_TEMP/kinaiya-qualify" \
        --evidence "$RUNNER_TEMP/linux-install-evidence.json"

The script builds *local, unqualified* nightly fixtures from the exact checked-out
commit, then uses the production installer functions and the installed launcher.
It never publishes a release, contacts a model provider, starts an Agent task,
uses ``sudo`` or weakens sandbox preflight. Runtime preparation downloads locked
wheels from PyPI exactly as a real installation does.

Every check records an observed outcome. A refused preflight is reported as a
refusal and exits non-zero; it is never converted into a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]
# Fixed loopback test ports outside the managed defaults (8760-8765), so the
# qualification never collides with, or stops, an owner's real services.
TEST_PORTS = {"session": 18760, "agent": 18761}


class QualificationError(AssertionError):
    """A required lifecycle observation did not hold."""


def _require(condition, message):
    if not condition:
        raise QualificationError(message)


def _git_head():
    return subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()


def _run(argv, *, env=None, timeout=120, check=True):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    if check and result.returncode:
        raise QualificationError(
            f"{Path(argv[0]).name} exited {result.returncode}: {result.stderr[-1500:]}"
        )
    return result


def _python_snippet(code, *, env=None, timeout=900, check=True):
    """Run installer code in a child so kills/locks are real process events."""
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONPATH"] = str(REPO)
    return _run([sys.executable, "-c", code], env=child_env, timeout=timeout, check=check)


def _file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _launcher_version(layout):
    result = _run([str(layout.bin / "kinaiya"), "version"], timeout=60)
    return json.loads(result.stdout)


class Qualification:
    def __init__(self, root: Path):
        from services.distribution.installation import Layout

        self.root = root
        self.layout = Layout.for_home(root / "home")
        self.sha = _git_head()
        self.checks: list[dict] = []
        self.bundles: dict[int, Path] = {}
        self.releases: dict[int, dict] = {}

    def record(self, name, outcome, **detail):
        entry = {"check": name, "outcome": outcome, **detail}
        self.checks.append(entry)
        print(json.dumps(entry), flush=True)

    # -- fixtures -----------------------------------------------------------

    def build_bundles(self, numbers=(1, 2, 3)):
        from scripts.build_release import build_release, validate_bundle

        for number in numbers:
            bundle = self.root / f"bundle-{number}"
            release = build_release(
                REPO,
                bundle,
                channel="nightly",
                version="0.1.0",
                build_number=number,
                source_sha=self.sha,
            )
            validate_bundle(bundle)
            self.bundles[number], self.releases[number] = bundle, release
        self.record("local_fixture_bundles", "built", builds=list(numbers), source_sha=self.sha)

    def archive(self, number):
        from services.distribution.installation import source_artifact

        release = self.releases[number]
        return self.bundles[number] / source_artifact(release)["name"]

    def install(self, number):
        from services.distribution.installation import install_release

        return install_release(self.layout, self.releases[number], self.archive(number))

    def active_generation(self):
        return (self.layout.install / "current").resolve().name

    # -- checks -------------------------------------------------------------

    def preflight_without_bubblewrap(self):
        """Real preflight code with bwrap absent from PATH must refuse before writes."""
        shim = self.root / "no-bwrap-path"
        shim.mkdir()
        os.symlink(sys.executable, shim / "python3")
        env = {**os.environ, "PATH": str(shim)}
        probe_home = self.root / "missing-bwrap-home"
        result = _python_snippet(
            "from pathlib import Path\n"
            "from services.distribution.installation import Layout, preflight, InstallError\n"
            f"layout = Layout.for_home(Path({str(probe_home)!r}))\n"
            "try:\n"
            "    preflight(layout)\n"
            "except InstallError as exc:\n"
            "    print('REFUSED', exc)\n"
            "else:\n"
            "    raise SystemExit('preflight accepted a host without bubblewrap')\n",
            env=env,
        )
        _require("REFUSED" in result.stdout and "bubblewrap" in result.stdout, result.stdout)
        _require(not probe_home.exists(), "refused preflight wrote installer state")
        self.record("preflight_missing_bubblewrap", "refused", message=result.stdout.strip())

    def preflight(self):
        from services.distribution.installation import InstallError, preflight

        try:
            result = preflight(self.layout)
        except InstallError as exc:
            self.record("preflight", "refused", message=str(exc))
            raise
        self.record("preflight", "passed", **result)

    def tampered_digest_refused(self):
        from services.distribution.installation import install_release

        tampered = self.root / "tampered.tar.gz"
        data = bytearray(self.archive(1).read_bytes())
        data[len(data) // 2] ^= 0xFF
        tampered.write_bytes(bytes(data))
        try:
            install_release(self.layout, self.releases[1], tampered)
        except Exception as exc:  # noqa: BLE001 -- any refusal is acceptable, success is not
            message = f"{type(exc).__name__}: {exc}"
        else:
            raise QualificationError("tampered archive was installed")
        _require(not os.path.lexists(self.layout.install / "current"), "tampered install switched")
        _require(
            not (self.layout.install / "pending.json").exists(), "tampered install left journal"
        )
        self.record("invalid_digest", "refused", message=message[:300])

    def clean_install(self):
        self.install(1)
        version = _launcher_version(self.layout)
        _require(version["release"]["build_number"] == 1, f"unexpected version {version}")
        self.generation_1 = self.active_generation()
        self.record("clean_install", "passed", generation=self.generation_1, build_number=1)

    def rerun_is_current(self):
        before = self.active_generation()
        self.install(1)
        _require(self.active_generation() == before, "rerun replaced current generation")
        self.record("rerun_same_release", "current", generation=before)

    def configure(self):
        # Use the installed launcher: setup imports runtime dependencies that live
        # only in the managed environment, exactly as an owner's `kinaiya setup`.
        argv = [str(self.layout.bin / "kinaiya"), "setup", "--yes", "--endpoint"]
        argv += ["http://127.0.0.1:8080/v1", "--model", "qualification-no-provider"]
        result = json.loads(_run(argv, timeout=120).stdout)
        _require(
            result["ready"] is False and result["provider_connection"] == "not_checked", result
        )
        config = json.loads(self.layout.config.read_text())
        config["session_url"] = f"http://127.0.0.1:{TEST_PORTS['session']}"
        config["agent_url"] = f"http://127.0.0.1:{TEST_PORTS['agent']}"
        self.layout.config.write_text(json.dumps(config, indent=2) + "\n")
        self.config_digest = _file_digest(self.layout.config)
        self.record("agent_only_setup", "configured", provider_contacted=False)

    def interrupted_update_recovers(self):
        """SIGKILL during real staging; the journal must block blind retry until recover."""
        from services.distribution.installation import InstallError, recover

        marker = self.root / "staging-started"
        code = (
            "import json, time\n"
            "from pathlib import Path\n"
            "from services.distribution.installation import Layout, install_release\n"
            f"layout = Layout.for_home(Path({str(self.root / 'home')!r}))\n"
            f"release = json.loads(Path({str(self.bundles[2] / 'release-manifest.json')!r}).read_text())\n"
            "def stalled(generation, source):\n"
            f"    Path({str(marker)!r}).write_text(generation.name)\n"
            "    time.sleep(600)\n"
            f"install_release(layout, release, Path({str(self.archive(2))!r}), prepare=stalled)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(REPO)}
        child = subprocess.Popen([sys.executable, "-c", code], env=env)
        try:
            deadline = time.monotonic() + 120
            while not marker.exists():
                _require(child.poll() is None, f"staging child exited early ({child.returncode})")
                _require(time.monotonic() < deadline, "staging did not start")
                time.sleep(0.1)
            os.kill(child.pid, signal.SIGKILL)
        finally:
            child.wait(timeout=30)
        _require((self.layout.install / "pending.json").exists(), "no journal after interruption")
        _require(self.active_generation() == self.generation_1, "interrupted stage switched")
        try:
            self.install(2)
        except InstallError as exc:
            blocked = str(exc)
        else:
            raise QualificationError("update proceeded over an interrupted journal")
        result = recover(self.layout)
        _require(result["action"] == "discarded_stage", result)
        _require(
            not (self.layout.install / "generations" / marker.read_text()).exists(), "stage kept"
        )
        self.record(
            "interrupted_update", "recovered", blocked_retry=blocked, recovery=result["action"]
        )

    def concurrent_update_refused(self):
        from services.distribution.installation import operation_lock

        with operation_lock(self.layout.install / "runtime.lock"):
            result = _python_snippet(
                "import json\n"
                "from pathlib import Path\n"
                "from services.distribution.installation import Layout, install_release, InstallError\n"
                f"layout = Layout.for_home(Path({str(self.root / 'home')!r}))\n"
                f"release = json.loads(Path({str(self.bundles[2] / 'release-manifest.json')!r}).read_text())\n"
                "try:\n"
                f"    install_release(layout, release, Path({str(self.archive(2))!r}))\n"
                "except InstallError as exc:\n"
                "    print('REFUSED', exc)\n"
                "else:\n"
                "    raise SystemExit('concurrent update acquired the lock')\n"
            )
        _require("REFUSED" in result.stdout, result.stdout)
        self.record("concurrent_update", "refused", message=result.stdout.strip())

    def update(self):
        self.install(2)
        version = _launcher_version(self.layout)
        _require(version["release"]["build_number"] == 2, f"unexpected version {version}")
        _require(_file_digest(self.layout.config) == self.config_digest, "update changed config")
        self.generation_2 = self.active_generation()
        _require(self.generation_2 != self.generation_1, "update reused generation")
        self.record("update", "passed", generation=self.generation_2, config_preserved=True)

    def dirty_source_refused(self):
        from services.distribution.installation import InstallError

        source = self.layout.install / "current" / "source" / "README.md"
        original = source.read_bytes()
        source.write_bytes(original + b"\nlocal modification\n")
        try:
            self.install(3)
        except InstallError as exc:
            message = str(exc)
        else:
            raise QualificationError("update replaced locally modified source")
        finally:
            source.write_bytes(original)
        _require(self.active_generation() == self.generation_2, "dirty refusal switched")
        self.record("dirty_source_update", "refused", message=message)

    def older_release_refused(self):
        from services.distribution.installation import InstallError

        try:
            self.install(1)
        except InstallError as exc:
            message = str(exc)
        else:
            raise QualificationError("older release was installed as an update")
        self.record("downgrade", "refused", message=message)

    def sandbox_denials(self):
        """The installed runtime's real Agent shell sandbox must deny host escapes."""
        workspace = self.root / "sandbox-workspace"
        workspace.mkdir()
        outside = self.root / "outside-proof"
        source = self.layout.install / "current" / "source"
        python = self.layout.install / "current" / "runtime" / "bin" / "python"
        code = (
            "import json, subprocess, sys\n"
            "from pathlib import Path\n"
            "from core.policy import ToolPolicy\n"
            "from services.agent.tools import build_bwrap_args\n"
            f"policy = ToolPolicy(Path({str(workspace)!r}))\n"
            # Each probe prints a marker only if the forbidden effect succeeded, so a
            # missing interpreter or unrelated startup failure cannot pose as denial.
            "attempts = {\n"
            f"  'write_outside_workspace': 'echo x > {outside} && echo ESCAPED',\n"
            "  'write_system_root': 'touch /usr/kinaiya-qualification-proof && echo ESCAPED',\n"
            "  'network_without_grant': 'python3 -c \"import socket; socket.create_connection((\\'1.1.1.1\\', 443), 3); print(\\'ESCAPED\\')\"',\n"
            "  'interpreter_available': 'python3 -c \"print(\\'READY\\')\"',\n"
            "}\n"
            "results = {}\n"
            "for name, command in attempts.items():\n"
            "    argv, isolated = build_bwrap_args(policy, command, False)\n"
            "    assert isolated, name\n"
            "    p = subprocess.run(argv, capture_output=True, text=True, timeout=30)\n"
            "    results[name] = {'exit': p.returncode, 'stdout': p.stdout.strip()[-200:],\n"
            "                     'stderr': p.stderr.strip()[-300:]}\n"
            "argv, _ = build_bwrap_args(policy, 'echo inside > /workspace/allowed.txt', False)\n"
            "p = subprocess.run(argv, capture_output=True, text=True, timeout=30)\n"
            "results['write_inside_workspace'] = {'exit': p.returncode, 'stderr': p.stderr.strip()[-300:]}\n"
            "print(json.dumps(results))\n"
        )
        env = {**os.environ, "PYTHONPATH": str(source)}
        result = _run([str(python), "-c", code], env=env, timeout=120)
        probes = json.loads(result.stdout.strip().splitlines()[-1])
        inside = probes.pop("write_inside_workspace")
        _require(inside["exit"] == 0, f"granted workspace write failed: {inside}")
        _require(
            (workspace / "allowed.txt").read_text().strip() == "inside", "workspace write lost"
        )
        ready = probes.pop("interpreter_available")
        _require(ready["exit"] == 0 and ready["stdout"] == "READY", f"sandbox python: {ready}")
        escaped = {k: v for k, v in probes.items() if v["exit"] == 0 or "ESCAPED" in v["stdout"]}
        _require(not escaped, f"sandbox escape succeeded: {escaped}")
        _require(not outside.exists(), "write outside workspace reached the host")
        _require(not Path("/usr/kinaiya-qualification-proof").exists(), "system root was written")
        self.record(
            "sandbox_denials",
            "denied",
            probes={k: {"exit": v["exit"], "stderr": v["stderr"]} for k, v in probes.items()},
            workspace_write="allowed",
        )

    def services(self):
        from services.distribution.installation import InstallError, operation_lock

        for port in TEST_PORTS.values():
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", port))  # fail early instead of touching a live service
        processes, logs, observed = [], [], {}
        try:
            for service, port in TEST_PORTS.items():
                log = (self.root / f"{service}.log").open("wb")
                logs.append(log)
                process = subprocess.Popen(
                    [str(self.layout.bin / "kinaiya"), "serve", service, "--port", str(port)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(process)
                for _ in range(300):
                    _require(process.poll() is None, f"{service} exited during startup")
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                            health = json.load(response)
                        break
                    except (OSError, HTTPError, ValueError):
                        time.sleep(0.1)
                else:
                    raise QualificationError(f"{service} health deadline exceeded")
                _require(
                    health.get("service") == service and health.get("status") == "ready", health
                )
                observed[service] = "ready"
            try:
                urlopen(f"http://127.0.0.1:{TEST_PORTS['session']}/v1/sessions", timeout=3)
            except HTTPError as exc:
                _require(exc.code in (401, 403), f"unexpected unauthenticated status {exc.code}")
                observed["unauthenticated_owner_read"] = exc.code
            else:
                raise QualificationError("unauthenticated owner read was allowed")
            try:
                with operation_lock(self.layout.install / "runtime.lock"):
                    raise QualificationError("updater lock acquired while services run")
            except InstallError:
                observed["update_lock"] = "blocked_while_serving"
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for log in logs:
                log.close()
        self.record("services", "passed", **observed, provider_contacted=False, tasks_started=0)

    def _cli(self, *args):
        # Mutating commands print their reviewed plan line before the JSON result.
        out = _run([str(self.layout.bin / "kinaiya"), *args], timeout=120).stdout
        start = out.find("{")
        _require(start >= 0, f"no JSON result from kinaiya {args[0]}")
        return json.loads(out[start:])

    def rollback(self):
        result = self._cli("rollback", "--generation", self.generation_1, "--yes")
        _require(_launcher_version(self.layout)["release"]["build_number"] == 1, "rollback failed")
        _require(result["database_restored"] is False, result)
        result_forward = self._cli("rollback", "--generation", self.generation_2, "--yes")
        _require(
            _launcher_version(self.layout)["release"]["build_number"] == 2, "roll forward failed"
        )
        self.record(
            "compatible_rollback",
            "passed",
            to=self.generation_1,
            back_to=self.generation_2,
            action=result_forward["action"],
            database_restored=False,
        )

    def uninstall(self):
        result = self._cli("uninstall", "--yes")
        _require(not (self.layout.bin / "kinaiya").exists(), "launcher remained after uninstall")
        _require(self.layout.config.is_file() and self.layout.data.is_dir(), "user data removed")
        _require(_file_digest(self.layout.config) == self.config_digest, "config changed")
        self.record("uninstall", "passed", data_retained=True, **result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="New empty disposable directory")
    parser.add_argument("--evidence", type=Path, required=True, help="JSON evidence output path")
    args = parser.parse_args(argv)
    if platform.system() != "Linux":
        parser.exit(2, "qualification requires native Linux\n")
    if args.root.exists():
        parser.exit(2, "--root must not exist; use a new disposable directory\n")
    args.root.mkdir(parents=True)
    os.environ.setdefault("TMPDIR", str(args.root))
    qualification = Qualification(args.root.resolve())
    host = platform.freedesktop_os_release()
    evidence = {
        "source_sha": qualification.sha,
        "host": {
            "distro": f"{host.get('ID')} {host.get('VERSION_ID')}",
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "runner": os.environ.get("RUNNER_NAME", "unknown"),
            "image": os.environ.get("ImageOS", "unknown"),
        },
        "fixtures": "local unqualified nightly bundles built from source_sha; not a published release",
        "checks": qualification.checks,
    }
    steps = [
        qualification.preflight_without_bubblewrap,
        qualification.preflight,
        qualification.build_bundles,
        qualification.tampered_digest_refused,
        qualification.clean_install,
        qualification.rerun_is_current,
        qualification.configure,
        qualification.interrupted_update_recovers,
        qualification.concurrent_update_refused,
        qualification.update,
        qualification.dirty_source_refused,
        qualification.older_release_refused,
        qualification.sandbox_denials,
        qualification.services,
        qualification.rollback,
        qualification.uninstall,
    ]
    status = 0
    try:
        for step in steps:
            step()
        evidence["result"] = "passed"
    except Exception as exc:  # noqa: BLE001 -- record the exact failure as evidence
        evidence["result"] = "failed"
        evidence["failure"] = f"{type(exc).__name__}: {exc}"[:2000]
        status = 1
    finally:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"result": evidence["result"], "failure": evidence.get("failure")}))
        # Leave generated runtimes for inspection only on failure.
        if status == 0:
            shutil.rmtree(args.root, ignore_errors=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
