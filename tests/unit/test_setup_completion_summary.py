"""Issue #397: Setup completion summary, truthful readiness, and immediate next action."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services import launcher
from services.distribution import cli, wizard, wizard_summary
from services.distribution.installation import Layout
from services.distribution.wizard_summary import (
    format_json_receipt,
    render_completion_summary,
    sanitize_completion_receipt,
)


def _base_receipt(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    layout = Layout.for_home(tmp_path)
    base: dict[str, Any] = {
        "install": str(layout.install),
        "launcher": str(layout.bin / "kinaiya"),
        "config": str(layout.config),
        "data": str(layout.data),
        "workspace": str(layout.workspace),
        "adapter": "lmstudio",
        "endpoint": "http://127.0.0.1:1234",
        "model": "qwen2.5-coder-7b",
        "converse": False,
        "provider_connection": "not_checked",
        "ready": False,
        "next": "kinaiya serve host",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "overrides,expected_badge,expected_ready,expected_state",
    [
        (
            {"provider_connection": "not_checked", "ready": False},
            "Configured (not tested)",
            False,
            "configured_untested",
        ),
        (
            {"provider_connection": "skipped", "ready": False},
            "Configured (not tested)",
            False,
            "configured_untested",
        ),
        (
            {
                "provider_connection": "verified",
                "connection_detail": "HTTP 200 (14ms)",
                "ready": False,
            },
            "Connection verified (HTTP 200 (14ms))",
            False,
            "connection_verified",
        ),
        (
            {
                "provider_connection": "failed",
                "connection_error": "Connection refused on 127.0.0.1:1234",
                "ready": False,
            },
            "Connection check failed (Connection refused on 127.0.0.1:1234)",
            False,
            "connection_failed",
        ),
        (
            {"provider_connection": "verified", "host_running": True},
            "Host running",
            True,
            "host_running",
        ),
    ],
)
def test_readiness_badges_are_truthful_and_never_claim_ready_when_untested(
    tmp_path: Path,
    overrides: dict[str, Any],
    expected_badge: str,
    expected_ready: bool,
    expected_state: str,
) -> None:
    receipt = _base_receipt(tmp_path, **overrides)
    sanitized = sanitize_completion_receipt(receipt)

    assert sanitized["ready"] is expected_ready
    assert sanitized["readiness_state"] == expected_state
    assert sanitized["readiness_badge"] == expected_badge

    text = render_completion_summary(receipt, is_tty=False)
    assert f"Status: {expected_badge}" in text
    assert "Status: Ready" not in text

    # In color TTY mode, untested or failed states must never use green (\x1b[32m)
    colored = render_completion_summary(receipt, is_tty=True, no_color=False, env={})
    if expected_state in {"configured_untested", "connection_failed"}:
        assert "\x1b[32m" not in colored


def test_agent_only_vs_optional_converse_and_web_assets_warning(tmp_path: Path) -> None:
    agent_only = _base_receipt(tmp_path, converse=False, web_assets_available=True)
    out_agent = render_completion_summary(agent_only, is_tty=False)
    assert "Converse: Disabled (Agent-only)" in out_agent
    assert "Web UI assets: missing" not in out_agent

    with_converse = _base_receipt(
        tmp_path,
        converse=True,
        converse_model="llama-3.1-converse",
        web_assets_available=False,
    )
    out_converse = render_completion_summary(with_converse, is_tty=False)
    assert "Converse: Enabled (llama-3.1-converse)" in out_converse
    assert "Web UI assets: missing (run 'flutter build web' in apps/kinaiya_flutter" in out_converse
    assert "kinaiya serve host (or kinaiya start)" in out_converse
    assert "kinaiya doctor | kinaiya setup model" in out_converse


def test_credential_export_step_omitted_when_saved_or_set_and_shown_when_unset(
    tmp_path: Path,
) -> None:
    # 1. Local provider without credential_env -> no export step
    local_out = render_completion_summary(_base_receipt(tmp_path), is_tty=False, env={})
    assert "export " not in local_out

    # 2. Cloud provider with credential_env unset in env -> includes export step
    cloud_unset = _base_receipt(
        tmp_path,
        adapter="meta",
        endpoint="https://api.example.com/v1",
        credential_env="PROVIDER_API_KEY",
    )
    unset_out = render_completion_summary(cloud_unset, is_tty=False, env={})
    assert "1. export PROVIDER_API_KEY=<your-api-key>" in unset_out
    assert "2. Start host: kinaiya serve host (or kinaiya start)" in unset_out

    # 3. Cloud provider with credential_env already present in env -> omits export step
    set_out = render_completion_summary(
        cloud_unset, is_tty=False, env={"PROVIDER_API_KEY": "already-set"}
    )
    assert "export PROVIDER_API_KEY" not in set_out
    assert "1. Start host: kinaiya serve host (or kinaiya start)" in set_out

    # 4. Masked/saved local credential -> omits export step even if env is empty
    masked_saved = _base_receipt(
        tmp_path,
        adapter="meta",
        endpoint="https://api.example.com/v1",
        credential_env="PROVIDER_API_KEY",
        credential_saved=True,
    )
    saved_out = render_completion_summary(masked_saved, is_tty=False, env={})
    assert "export PROVIDER_API_KEY" not in saved_out


def test_narrow_tty_no_color_and_secret_redaction_in_text_and_json(tmp_path: Path) -> None:
    receipt = _base_receipt(
        tmp_path,
        adapter="meta",
        endpoint="https://owner:sk-supersecretkey123456@api.example.com:8443/v1?api_key=sk-secret99999999#frag",
        model="cloud-model",
        provider_connection="failed",
        connection_error="Auth rejected Bearer sk- leakedtoken987654321",
    )

    # Narrow 40-column TTY with NO_COLOR=1
    narrow = render_completion_summary(
        receipt, is_tty=True, no_color=False, width=40, env={"NO_COLOR": "1"}
    )
    assert "\x1b" not in narrow
    assert "sk-supersecretkey123456" not in narrow
    assert "sk-secret99999999" not in narrow
    assert "leakedtoken987654321" not in narrow
    assert "https://api.example.com:8443/v1" in narrow
    for line in narrow.splitlines():
        assert len(line) <= 40

    # Machine-readable JSON snapshot contains zero secrets and zero ANSI escapes
    json_str = format_json_receipt(receipt)
    assert "\x1b" not in json_str
    assert "sk-" not in json_str
    assert "owner:" not in json_str
    parsed = json.loads(json_str)
    assert parsed["ready"] is False
    assert parsed["provider_connection"] == "failed"
    assert parsed["readiness_state"] == "connection_failed"
    assert parsed["agent"]["endpoint"] == "https://api.example.com:8443/v1"
    assert parsed["next_command"] == "kinaiya serve host"


def test_rendering_never_mutates_files_and_wizard_outputs_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = Layout.for_home(tmp_path)
    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )

    # Rendering a summary before setup does not create or mutate any files
    preview = render_completion_summary(_base_receipt(tmp_path), is_tty=False)
    assert "Kinaiya Setup Summary" in preview
    assert not layout.config.exists()
    assert not layout.data.exists()

    class Terminal:
        def __init__(self) -> None:
            self.answers = iter(["", "", "", "summary-test-model", "no", "", "no", "yes"])
            self.output = ""

        def write(self, value: str) -> None:
            self.output += value

        def flush(self) -> None:
            pass

        def readline(self) -> str:
            return next(self.answers) + "\n"

    tty = Terminal()
    result = wizard.run_wizard(layout, tty=tty)
    assert "Kinaiya Setup Summary" in tty.output
    assert "Status: Configured (not tested)" in tty.output
    assert "Converse: Disabled (Agent-only)" in tty.output
    assert result["ready"] is False
    assert result["readiness_state"] == "configured_untested"
    assert result["agent"]["model"] == "summary-test-model"

    # Also verify noninteractive cli.dispatch(['setup', '--yes', ...]) returns structured receipt
    layout2 = Layout.for_home(tmp_path / "cli-home")
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout2)
    parser = launcher.build_parser()
    args = parser.parse_args(
        [
            "setup",
            "--yes",
            "--json",
            "--endpoint",
            "http://127.0.0.1:1234",
            "--model",
            "headless-model",
        ]
    )
    cli_receipt = cli.dispatch(args)
    assert cli_receipt["ready"] is False
    assert cli_receipt["readiness_state"] == "configured_untested"
    assert cli_receipt["config"] == str(layout2.config)
    assert cli_receipt["agent"]["model"] == "headless-model"
    assert hasattr(wizard_summary, "render_completion_summary")
