"""Issue #384: human-readable stage progress, quiet redacted logs, and deterministic JSON."""

from __future__ import annotations

import io
import json
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest
from test_linux_distribution import archive_release, distribution, linux_directory_links_on_windows  # noqa: F401

from services.distribution import cli
from services.distribution.stage_progress import (
    STAGES,
    StageReporter,
    activate_reporter,
    redact_install_log,
)


class FakeTTYStream(io.StringIO):
    def __init__(self, *, tty: bool = True) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_tty_stage_transitions_and_quiet_subprocess_logging(tmp_path):
    out = FakeTTYStream(tty=True)
    err = FakeTTYStream(tty=True)
    log_path = tmp_path / "cache" / "install.log"
    reporter = StageReporter(
        log_path=log_path,
        json_output=False,
        stream=out,
        err_stream=err,
        width=80,
        no_color=True,
    )
    assert [key for key, _ in STAGES] == [
        "preflight",
        "dependencies",
        "download",
        "staging",
        "setup_handoff",
    ]

    reporter.start_stage("preflight")
    reporter.complete_stage("preflight")

    reporter.start_stage("dependencies")
    reporter.log_subprocess(
        "dependencies",
        ["sudo", "apt-get", "install", "--no-upgrade", "-y", "bubblewrap"],
        0,
        stdout="Reading package lists... noisy apt output\nUnpacking bubblewrap...",
        stderr="",
    )
    reporter.complete_stage("dependencies", detail="installed bubblewrap")

    reporter.start_stage("download")
    reporter.update_download(512, 1024)
    reporter.update_download(1024, 1024)
    reporter.mark_sha256_verified("a" * 64)
    reporter.complete_stage("download")

    reporter.start_stage("staging")
    reporter.log_subprocess(
        "staging",
        ["python", "-m", "pip", "install", "-r", "requirements-runtime.lock"],
        0,
        stdout="Collecting httpx==0.28.1\nInstalling collected packages: httpx",
        stderr="",
    )
    reporter.complete_stage("staging")

    reporter.start_stage("setup_handoff")
    reporter.complete_stage("setup_handoff")

    tty_text = out.getvalue()
    assert "[1/5] Checking host & sandbox prerequisites ... PASS" in tty_text
    assert "[2/5] Verifying system dependencies (installed bubblewrap) ... PASS" in tty_text
    assert "[3/5] Download progress: 512/1024 B (50%) - awaiting SHA-256 verification" in tty_text
    assert "[3/5] Download progress: 1024/1024 B (100%) - awaiting SHA-256 verification" in tty_text
    assert "[3/5] Downloading & verifying release artifact ... PASS" in tty_text
    assert "[4/5] Staging runtime & isolated virtual environment ... PASS" in tty_text
    assert "[5/5] Handing off to Kinaiya setup ... PASS" in tty_text

    # Noisy apt and pip stdout must stay out of the main terminal screen:
    assert "Reading package lists" not in tty_text
    assert "Collecting httpx" not in tty_text

    # Full diagnostics are recorded in the private 0600 log file:
    assert log_path.is_file()
    if sys.platform == "linux":
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    log_text = log_path.read_text(encoding="utf-8")
    assert "Reading package lists" in log_text
    assert "Collecting httpx" in log_text
    assert "SHA-256 verification succeeded" in log_text


def test_unknown_size_never_invents_percentage_and_success_requires_sha256(tmp_path):
    out = FakeTTYStream(tty=True)
    log_path = tmp_path / "install.log"
    reporter = StageReporter(
        log_path=log_path,
        json_output=False,
        stream=out,
        width=80,
        no_color=True,
    )
    reporter.start_stage("download")
    reporter.update_download(4096, None)
    reporter.update_download(8192, 0)
    tty_text = out.getvalue()
    assert "4096 B (total size unknown)" in tty_text
    assert "8192 B (total size unknown)" in tty_text
    assert "%" not in tty_text
    assert "PASS" not in tty_text

    # Cannot mark download complete before SHA-256 verification succeeds:
    with pytest.raises(RuntimeError, match="SHA-256 verification"):
        reporter.complete_stage("download")
    assert "PASS" not in out.getvalue()

    reporter.mark_sha256_verified("f" * 64)
    reporter.complete_stage("download")
    assert "[3/5] Downloading & verifying release artifact ... PASS" in out.getvalue()


@pytest.mark.parametrize(
    "stage,reason,expected_remediation",
    [
        ("dependencies", "apt index out of date", "sudo apt-get install --no-upgrade"),
        ("download", "Digest mismatch on source.tar.gz", "github.com"),
        ("staging", "Runtime stage 2 failed", "pypi.org"),
    ],
)
def test_stage_failures_report_failing_stage_remediation_and_private_log(
    tmp_path, stage, reason, expected_remediation
):
    out = FakeTTYStream(tty=True)
    err = FakeTTYStream(tty=True)
    log_path = tmp_path / "private" / "install.log"
    reporter = StageReporter(
        log_path=log_path,
        json_output=False,
        stream=out,
        err_stream=err,
        width=140,
        no_color=True,
    )
    reporter.start_stage(stage)
    formatted = reporter.fail_stage(stage, reason)
    err_text = err.getvalue()
    assert f"({stage}) ... FAIL" in err_text
    assert f"Reason:         {reason}" in err_text
    assert f"Diagnostic log: {log_path} (mode 0600)" in err_text
    assert expected_remediation in err_text
    assert formatted.strip() == err_text.strip()
    assert "PASS" not in out.getvalue()
    assert log_path.is_file()
    if sys.platform == "linux":
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert reason in log_path.read_text(encoding="utf-8")


def test_secret_and_authorization_redaction_in_console_and_private_log(tmp_path):
    out = FakeTTYStream(tty=True)
    err = FakeTTYStream(tty=True)
    log_path = tmp_path / "install.log"
    reporter = StageReporter(
        log_path=log_path,
        json_output=False,
        stream=out,
        err_stream=err,
        width=100,
        no_color=True,
    )
    raw_secret_text = (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret\n"
        "https://builder:super-secret-pass@release-assets.githubusercontent.com/archive.tar.gz\n"
        "OPENAI_API_KEY=sk-proj-1234567890abcdefSECRET\n"
        '{"api_key": "raw-json-secret-value", "api_key_env": "SAFE_ENV_NAME"}\n'
        "--api-token ghp_1234567890abcdef1234567890abcdef"
    )
    redacted = redact_install_log(raw_secret_text)
    for forbidden in (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "super-secret-pass",
        "sk-proj-1234567890abcdefSECRET",
        "raw-json-secret-value",
        "ghp_1234567890abcdef1234567890abcdef",
    ):
        assert forbidden not in redacted
    assert "SAFE_ENV_NAME" in redacted
    assert "[REDACTED]" in redacted

    reporter.start_stage("download")
    reporter.log_subprocess("download", ["curl"], 1, stdout=raw_secret_text, stderr=raw_secret_text)
    reporter.fail_stage("download", f"Failed with {raw_secret_text}")

    combined = out.getvalue() + err.getvalue() + log_path.read_text(encoding="utf-8")
    for forbidden in (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "super-secret-pass",
        "sk-proj-1234567890abcdefSECRET",
        "raw-json-secret-value",
        "ghp_1234567890abcdef1234567890abcdef",
    ):
        assert forbidden not in combined


def test_non_tty_no_color_json_cancellation_and_narrow_width_40_cols(tmp_path):
    log_path = tmp_path / "install.log"

    # 1) Narrow width (40 columns) wraps cleanly without exceeding 40 chars per line:
    narrow_out = FakeTTYStream(tty=True)
    narrow_err = FakeTTYStream(tty=True)
    narrow_reporter = StageReporter(
        log_path=log_path,
        json_output=False,
        stream=narrow_out,
        err_stream=narrow_err,
        width=40,
        no_color=True,
    )
    plan_text = narrow_reporter.render_plan(
        {
            "channel": "stable",
            "source_sha": "a" * 40,
            "download_bytes": 2048,
            "install": str(tmp_path / ".local/share/kinaiya/install"),
        }
    )
    assert max(len(line) for line in plan_text.splitlines()) <= 40
    narrow_reporter.start_stage("preflight")
    narrow_reporter.complete_stage("preflight")
    narrow_reporter.update_download(1024, 2048)
    narrow_reporter.fail_stage("download", "Connection reset while reading release asset")
    narrow_reporter.cancel("User pressed Ctrl-C; no files changed")
    for line in (narrow_out.getvalue() + narrow_err.getvalue()).splitlines():
        assert len(line) <= 40

    # 2) NO_COLOR=1 suppresses ANSI escape sequences on a color-capable TTY:
    no_color_out = FakeTTYStream(tty=True)
    nc_reporter = StageReporter(
        log_path=log_path,
        json_output=False,
        stream=no_color_out,
        env={"NO_COLOR": "1"},
    )
    nc_reporter.start_stage("preflight")
    nc_reporter.complete_stage("preflight")
    assert "\x1b" not in no_color_out.getvalue()

    # 3) Non-TTY or --json produces deterministic JSON plan without ANSI or progress chatter:
    piped_out = FakeTTYStream(tty=False)
    json_reporter = StageReporter(
        log_path=log_path,
        json_output=True,
        stream=piped_out,
    )
    payload = {"channel": "stable", "source_sha": "b" * 40, "install": "/tmp/install"}
    rendered = json_reporter.render_plan(payload, indent=None)
    assert json.loads(rendered) == payload
    assert "\x1b" not in rendered
    json_reporter.start_stage("preflight")
    json_reporter.complete_stage("preflight")
    assert piped_out.getvalue() == ""


def test_cli_and_bootstrap_support_json_flag_and_stage_progress(tmp_path, monkeypatch):
    module = distribution()
    layout = module.Layout.for_home(tmp_path)
    archive, release = archive_release(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)
    monkeypatch.setattr(cli, "preflight", lambda chosen, **kw: {"sandbox_probe": "passed"})
    monkeypatch.setattr(cli, "resolve_release", lambda channel: release)
    monkeypatch.setattr(cli, "open_release_url", lambda url: io.BytesIO(archive.read_bytes()))
    monkeypatch.setattr(
        cli,
        "install_release",
        lambda chosen, target, path: module.install_release(
            chosen, target, path, prepare=lambda *a: None, busy=lambda: False
        ),
    )

    parser = cli.argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    cli.configure(sub)
    parsed = parser.parse_args(["install", "--yes", "--no-setup", "--json"])
    assert parsed.json is True

    result = cli.dispatch(parsed)
    assert result["ready"] is False
    assert Path(result["log"]).is_file()
    if sys.platform == "linux":
        assert stat.S_IMODE(Path(result["log"]).stat().st_mode) == 0o600

    # Verify standalone install.sh parser and stage reporter
    script = Path(__file__).resolve().parents[2] / "install.sh"
    source = (
        script.read_text()
        .split("# BEGIN PYTHON BOOTSTRAP\n", 1)[1]
        .split("\n# END PYTHON BOOTSTRAP", 1)[0]
    )
    boot = ModuleType("bootstrap_stage_test")
    exec(compile(source, str(script), "exec"), boot.__dict__)
    assert [k for k, _ in boot.STAGES] == [
        "preflight",
        "dependencies",
        "download",
        "staging",
        "setup_handoff",
    ]
    with activate_reporter(None):
        assert boot.redact_install_log("Bearer secret12345") == "Bearer [REDACTED]"
