"""Issue #387: Setup wizard minimal first-run path with advanced host settings deferred."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from services import launcher
from services.distribution import cli, wizard, wizard_flow
from services.distribution.installation import InstallError, Layout


class FakeTerminal:
    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.output = ""
        self.prompts: list[str] = []

    def write(self, value: str) -> None:
        self.output += value
        self.prompts.append(value)

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        try:
            return next(self._answers) + "\n"
        except StopIteration:
            return ""


@pytest.fixture
def patch_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )


def test_quick_setup_default_path_uses_safe_defaults_without_path_prompts(
    tmp_path: Path, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    # Only 5 answers: endpoint, model, converse, advanced, confirm
    tty = FakeTerminal(["", "local-qwen-coder", "no", "no", "yes"])

    result = wizard.run_quick_wizard(layout, tty=tty)

    assert result["ready"] is False
    assert result["provider_connection"] == "not_checked"
    # Should not prompt for Authorized workspace or Private data directory in quick path
    assert "Authorized workspace [" not in tty.output
    assert "Private data directory [" not in tty.output
    # Should display safe default paths on the review screen before confirmation
    assert f"Planned config: {layout.config}" in tty.output
    assert f"Data: {layout.data}" in tty.output
    assert f"Workspace: {layout.workspace}" in tty.output
    assert "Converse: disabled (Agent-only)" in tty.output

    config = json.loads(layout.config.read_text(encoding="utf-8"))
    assert config["providers"]["agent"]["kind"] == "lmstudio"
    assert config["providers"]["agent"]["base_url"] == "http://127.0.0.1:1234"
    assert config["providers"]["agent"]["model"] == "local-qwen-coder"
    assert config["providers"]["agent"]["allow_cloud"] is False
    assert config["distribution"]["converse"] is False
    assert "voice" not in config


def test_quick_setup_cloud_endpoint_prompts_disclosure_and_credential_env(
    tmp_path: Path, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    # Cloud endpoint triggers allow_cloud + credential_env prompts in Quick Setup
    tty = FakeTerminal(
        [
            "meta=https://api.example.com/v1",
            "cloud-agent-model",
            "y",
            "CLOUD_API_KEY",
            "n",
            "n",
            "y",
        ]
    )

    result = wizard.run_wizard(layout, tty=tty, mode="quick")

    assert result["ready"] is False
    config = json.loads(layout.config.read_text(encoding="utf-8"))
    assert config["providers"]["agent"]["kind"] == "meta"
    assert config["providers"]["agent"]["base_url"] == "https://api.example.com/v1"
    assert config["providers"]["agent"]["model"] == "cloud-agent-model"
    assert config["providers"]["agent"]["allow_cloud"] is True
    assert config["providers"]["agent"]["api_key_env"] == "CLOUD_API_KEY"
    assert config["distribution"]["converse"] is False


def test_quick_setup_optional_converse_without_advanced_uses_agent_defaults(
    tmp_path: Path, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    tty = FakeTerminal(["", "shared-local-model", "yes", "no", "yes"])

    wizard.run_quick_wizard(layout, tty=tty)

    assert "Converse: enabled" in tty.output
    config = json.loads(layout.config.read_text(encoding="utf-8"))
    assert config["distribution"]["converse"] is True
    assert config["providers"]["converse"]["kind"] == "lmstudio"
    assert config["providers"]["converse"]["model"] == "shared-local-model"
    assert config["providers"]["decisions"]["kind"] == "compatible"
    assert config["providers"]["decisions"]["model"] == "shared-local-model"


def test_quick_setup_opting_into_advanced_customizes_paths_and_role_adapters(
    tmp_path: Path, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    custom_workspace = tmp_path / "CustomWorkspace"
    custom_data = tmp_path / "CustomData"
    tty = FakeTerminal(
        [
            "http://127.0.0.1:1234",
            "agent-model",
            "yes",  # enable converse
            "yes",  # open advanced settings
            str(custom_workspace),
            str(custom_data),
            "no",  # allow_cloud in advanced
            "",  # credential_env in advanced
            "compatible",  # converse adapter
            "http://127.0.0.1:8081/v1",  # converse endpoint
            "converse-custom-model",  # converse model
            "no",  # independent Converse cloud disclosure
            "",  # independent Converse credential variable name
            "native_d1",  # decisions adapter
            "http://127.0.0.1:8082",  # decisions endpoint
            "decisions-d1-model",  # decisions model
            "no",  # independent Decisions cloud disclosure
            "",  # independent Decisions credential variable name
            "yes",  # confirm write
        ]
    )

    wizard.run_quick_wizard(layout, tty=tty)

    assert f"Workspace: {custom_workspace}" in tty.output
    assert f"Data: {custom_data}" in tty.output
    assert custom_workspace.is_dir()
    assert custom_data.is_dir()
    config = json.loads(layout.config.read_text(encoding="utf-8"))
    assert config["providers"]["agent"]["model"] == "agent-model"
    assert config["providers"]["converse"]["kind"] == "compatible"
    assert config["providers"]["converse"]["base_url"] == "http://127.0.0.1:8081/v1"
    assert config["providers"]["converse"]["model"] == "converse-custom-model"
    assert config["providers"]["decisions"]["kind"] == "native_d1"
    assert config["providers"]["decisions"]["base_url"] == "http://127.0.0.1:8082"
    assert config["providers"]["decisions"]["model"] == "decisions-d1-model"


def test_quick_setup_cancel_before_write_leaves_disk_clean(
    tmp_path: Path, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    tty = FakeTerminal(["", "local-model", "no", "no", "no"])

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_quick_wizard(layout, tty=tty)

    assert not layout.config.exists()
    assert not layout.data.exists()
    assert not layout.workspace.exists()


def test_quick_setup_refuses_existing_config_without_clobbering(
    tmp_path: Path, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    layout.config.parent.mkdir(parents=True, exist_ok=True)
    original = '{"existing": true}\n'
    layout.config.write_text(original, encoding="utf-8")

    tty = FakeTerminal(["", "new-model", "no", "no", "yes"])
    with pytest.raises(InstallError, match="Preserving existing configuration"):
        wizard.run_quick_wizard(layout, tty=tty)

    assert layout.config.read_text(encoding="utf-8") == original
    assert tty.output == ""


def test_standard_wizard_allows_switching_to_quick_mode_or_cli_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    tty = FakeTerminal(["quick", "", "switched-quick-model", "no", "no", "yes"])

    wizard.run_wizard(layout, tty=tty)
    config = json.loads(layout.config.read_text(encoding="utf-8"))
    assert config["providers"]["agent"]["model"] == "switched-quick-model"

    # Also verify CLI `--quick` dispatches mode="quick"
    layout2 = Layout.for_home(tmp_path / "second")
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout2)

    @contextmanager
    def fake_open_terminal():
        yield FakeTerminal(["", "cli-quick-model", "no", "no", "yes"])

    monkeypatch.setattr(wizard, "open_terminal", fake_open_terminal)
    monkeypatch.setattr(wizard_flow, "open_terminal", fake_open_terminal)

    parser = launcher.build_parser()
    args = parser.parse_args(["setup", "--quick"])
    result = cli.dispatch(args)
    assert result["ready"] is False
    config2 = json.loads(layout2.config.read_text(encoding="utf-8"))
    assert config2["providers"]["agent"]["model"] == "cli-quick-model"


def test_noninteractive_setup_yes_and_unsupported_mode_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch_tokens: None
) -> None:
    layout = Layout.for_home(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)

    parser = launcher.build_parser()
    args = parser.parse_args(
        ["setup", "--yes", "--endpoint", "http://127.0.0.1:1234", "--model", "ci-model"]
    )
    result = cli.dispatch(args)
    assert result["ready"] is False
    assert json.loads(layout.config.read_text(encoding="utf-8"))["providers"]["agent"]["model"] == (
        "ci-model"
    )

    with pytest.raises(InstallError, match="Unsupported wizard mode"):
        wizard.run_wizard(Layout.for_home(tmp_path / "bad"), tty=FakeTerminal([]), mode="invalid")
