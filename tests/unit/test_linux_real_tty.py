"""Exercise actual controlling-terminal consent, not a seekable StringIO fake."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("script") is None,
    reason="Requires a real Linux controlling PTY (util-linux script)",
)


def run_tty(code, answers, home):
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(ROOT))
    env.pop("KINAIYA_INSTALL_ROOT", None)
    result = subprocess.run(
        [
            "script",
            "--quiet",
            "--return",
            "--command",
            shlex.join([sys.executable, "-c", code]),
            "/dev/null",
        ],
        input=answers,
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_cli_confirmation_uses_a_nonseekable_controlling_terminal(tmp_path):
    output = run_tty(
        "from services.distribution.cli import confirm; confirm(False, 'Test consent'); print('CONSENT_READ')",
        "yes\n",
        tmp_path,
    )
    assert "CONSENT_READ" in output


def test_setup_wizard_works_on_a_real_controlling_terminal(tmp_path):
    answers = "\n".join(["", "", "", "tty-no-inference", "no", "", "no", "yes"]) + "\n"
    output = run_tty(
        "from services.distribution.cli import main; main(['setup'])",
        answers,
        tmp_path,
    )
    assert "Kinaiya" in output and "Planned config" in output
    assert '"provider_connection": "not_checked"' in output
    config = json.loads((tmp_path / ".config/kinaiya/config.json").read_text())
    assert config["providers"]["agent"]["model"] == "tty-no-inference"
    assert config["policy"] == {
        "allow_write": False,
        "allow_shell": False,
        "network": False,
        "yolo": False,
    }
    assert config["distribution"]["converse"] is False


def test_standalone_bootstrap_reads_real_terminal_consent(tmp_path):
    # Isolate consent from network/install behavior. No fake release is published.
    source = (
        (ROOT / "install.sh")
        .read_text()
        .split("# BEGIN PYTHON BOOTSTRAP\n", 1)[1]
        .split("\n# END PYTHON BOOTSTRAP", 1)[0]
    )
    code = (
        f"ns={{'__name__':'bootstrap_unit'}}; exec(compile({source!r},'install.sh','exec'),ns); "
        "ns['preflight']=lambda:None; "
        "ns['resolve']=lambda channel:({'source_sha':'a'*40},{'size':1}); "
        "exec(\"def stop_download(*args):\\n print('BOOTSTRAP_CONSENT_READ')\\n raise SystemExit(0)\",ns); "
        "ns['download']=ns['stop_download']; ns['run'](['--no-setup'])"
    )
    assert "BOOTSTRAP_CONSENT_READ" in run_tty(code, "yes\n", tmp_path)
