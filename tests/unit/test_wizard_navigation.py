"""Issue #388: Setup wizard Back/Cancel navigation and targeted section reconfiguration."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from services import launcher
from services.distribution import cli, installation, wizard, wizard_navigation
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


def test_backtracking_across_steps_discards_stale_converse_and_rewinds_from_review(
    tmp_path: Path,
) -> None:
    layout = Layout.for_home(tmp_path)
    tty = FakeTerminal(
        [
            ":back",  # at step 0 (workspace): stays at step 0
            "",  # workspace (default)
            "",  # data (default)
            "http://127.0.0.1:1234",  # endpoint
            "typo-model",  # model (mistake)
            "back",  # at allow_cloud: rewind to model
            "first-pass-model",  # model (updated)
            "no",  # allow_cloud
            "",  # credential_env
            "yes",  # converse = yes (mistake)
            "compatible",  # converse_adapter
            "http://127.0.0.1:9999/v1",  # stale converse_endpoint
            "<",  # at converse_model: rewind to converse_endpoint
            ":back",  # rewind to converse_adapter
            "back",  # rewind to converse
            "no",  # change converse to no -> must discard stale converse answers!
            "back model",  # at review confirmation: rewind directly to model
            "final-verified-model",  # fix model
            "",  # allow_cloud (keeps "no")
            "",  # credential_env (keeps "")
            "",  # converse (keeps "no")
            "yes",  # confirm write
        ]
    )

    result = wizard.run_wizard(layout, tty=tty)

    assert result["ready"] is False
    assert "Already at the first step." in tty.output
    assert "Exact model name (required) [typo-model]: " in tty.output
    assert "Exact model name (required) [first-pass-model]: " in tty.output

    config = json.loads(layout.config.read_text(encoding="utf-8"))
    assert config["providers"]["agent"]["model"] == "final-verified-model"
    assert config["distribution"]["converse"] is False
    # Stale converse endpoint from the discarded branch must not be committed
    assert config["providers"]["converse"]["base_url"] != "http://127.0.0.1:9999/v1"


@pytest.mark.parametrize("cancel_token", ["cancel", ":cancel", ":q", "quit"])
def test_cancel_mid_wizard_and_at_confirmation_leaves_disk_unchanged(
    tmp_path: Path, cancel_token: str
) -> None:
    layout = Layout.for_home(tmp_path / cancel_token.lstrip(":"))
    tty_mid = FakeTerminal(["", "", "http://127.0.0.1:1234", cancel_token])

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_mid)

    assert not layout.config.exists()
    assert not layout.data.exists()
    assert not layout.workspace.exists()

    # Also test cancelling at confirmation when an existing config is present during section edit
    layout.config.parent.mkdir(parents=True, exist_ok=True)
    initial_bytes = b'{"preserve": "exact-bytes", "providers": {"agent": {"model": "keep"}}}\n'
    layout.config.write_bytes(initial_bytes)

    tty_confirm = FakeTerminal(["http://127.0.0.1:1234", "new-model", "no", "", cancel_token])
    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_confirm, section="model")

    assert layout.config.read_bytes() == initial_bytes


def test_targeted_section_reconfiguration_preserves_unrelated_settings_and_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = Layout.for_home(tmp_path)
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="initial-agent-model")

    # Add custom unrelated settings to config.json to prove section edits never clobber them
    cfg = json.loads(layout.config.read_text(encoding="utf-8"))
    cfg["policy"]["allow_write"] = True
    cfg["agent_profiles"] = {
        "fast": {
            "kind": "lmstudio",
            "base_url": "http://127.0.0.1:1234",
            "model": "fast-helper",
            "allow_cloud": False,
            "max_tokens": 128,
            "reasoning_effort": "none",
        }
    }
    cfg["skills"] = {"enabled": True}
    layout.config.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    tokens_path = layout.data / "service-tokens.json"
    original_tokens_bytes = tokens_path.read_bytes()

    # 1. Reconfigure `model` section only (4 prompts + confirm)
    tty_model = FakeTerminal(
        [
            "meta=https://api.example.com/v1",
            "updated-cloud-agent",
            "yes",
            "AGENT_CLOUD_KEY",
            "yes",
        ]
    )
    res_model = wizard.run_wizard(layout, tty=tty_model, section="model")
    assert res_model["section"] == "model"
    assert "Authorized workspace" not in tty_model.output
    assert "Enable optional Converse" not in tty_model.output

    cfg_after_model = json.loads(layout.config.read_text(encoding="utf-8"))
    assert cfg_after_model["providers"]["agent"]["kind"] == "meta"
    assert cfg_after_model["providers"]["agent"]["model"] == "updated-cloud-agent"
    assert cfg_after_model["providers"]["agent"]["api_key_env"] == "AGENT_CLOUD_KEY"
    # Unrelated settings and tokens are untouched
    assert cfg_after_model["policy"]["allow_write"] is True
    assert cfg_after_model["agent_profiles"] == cfg["agent_profiles"]
    assert cfg_after_model["skills"] == {"enabled": True}
    assert cfg_after_model["distribution"]["converse"] is False
    assert tokens_path.read_bytes() == original_tokens_bytes

    # 2. Reconfigure `converse` section only
    tty_converse = FakeTerminal(
        [
            "yes",
            "compatible",
            "http://127.0.0.1:8081/v1",
            "converse-local-model",
            "no",
            "",
            "native_d1",
            "http://127.0.0.1:8082",
            "decisions-d1-model",
            "no",
            "",
            "yes",
        ]
    )
    res_converse = wizard.run_wizard(layout, tty=tty_converse, section="converse")
    assert res_converse["section"] == "converse"
    assert "Authorized workspace" not in tty_converse.output
    assert "Agent provider endpoint" not in tty_converse.output

    cfg_after_converse = json.loads(layout.config.read_text(encoding="utf-8"))
    assert cfg_after_converse["distribution"]["converse"] is True
    assert cfg_after_converse["providers"]["converse"]["model"] == "converse-local-model"
    assert cfg_after_converse["providers"]["decisions"]["kind"] == "native_d1"
    assert cfg_after_converse["providers"]["decisions"]["model"] == "decisions-d1-model"
    # Agent and unrelated settings remain untouched
    assert cfg_after_converse["providers"]["agent"] == cfg_after_model["providers"]["agent"]
    assert cfg_after_converse["policy"]["allow_write"] is True
    assert cfg_after_converse["agent_profiles"] == cfg["agent_profiles"]
    assert tokens_path.read_bytes() == original_tokens_bytes

    # 3. Reconfigure `workspace` section only via CLI dispatch
    new_workspace = tmp_path / "ReconfiguredWorkspace"
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)

    @contextmanager
    def fake_open_terminal():
        yield FakeTerminal([str(new_workspace), "", "yes"])

    monkeypatch.setattr(wizard, "open_terminal", fake_open_terminal)
    parser = launcher.build_parser()
    args = parser.parse_args(["setup", "workspace"])
    res_ws = cli.dispatch(args)
    assert res_ws["section"] == "workspace"
    assert new_workspace.is_dir()

    cfg_after_ws = json.loads(layout.config.read_text(encoding="utf-8"))
    assert cfg_after_ws["distribution"]["workspace"] == str(new_workspace)
    assert cfg_after_ws["distribution"]["converse"] is True
    assert cfg_after_ws["providers"] == cfg_after_converse["providers"]
    assert tokens_path.read_bytes() == original_tokens_bytes


def test_transactional_rollback_restores_config_when_validation_or_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = Layout.for_home(tmp_path)
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="stable-model")
    before_bytes = layout.config.read_bytes()

    # 1. Validation failure (e.g. invalid adapter/endpoint in section update)
    tty_invalid = FakeTerminal(["http://user:secret@127.0.0.1:1234", "bad-ep", "no", "", "yes"])
    with pytest.raises(InstallError, match="credential-free loopback endpoint"):
        wizard.run_wizard(layout, tty=tty_invalid, section="model")
    assert layout.config.read_bytes() == before_bytes

    # 2. Simulated write failure after validation restores backup bytes
    def fail_atomic_json(path: Path, value: object) -> None:
        path.write_text("{corrupted-partial", encoding="utf-8")
        raise OSError("simulated disk write failure")

    monkeypatch.setattr(installation, "atomic_json", fail_atomic_json)
    tty_write_fail = FakeTerminal(["http://127.0.0.1:1234", "new-valid-model", "no", "", "yes"])
    with pytest.raises(OSError, match="simulated disk write failure"):
        wizard.run_wizard(layout, tty=tty_write_fail, section="model")

    assert layout.config.read_bytes() == before_bytes
    assert not list(layout.config.parent.glob("config.json.bak.*"))

    # 3. Simulated layout.json write failure after config.json write restores both files
    real_atomic_json = json.dumps
    layout.install.mkdir(parents=True, exist_ok=True, mode=0o700)
    layout_marker = layout.install / "layout.json"
    layout_marker.write_text(
        real_atomic_json({k: str(v) for k, v in layout.__dict__.items()}, indent=2) + "\n",
        encoding="utf-8",
    )
    before_layout_bytes = layout_marker.read_bytes()

    def fail_only_layout_json(path: Path, value: object) -> None:
        if path.name == "layout.json":
            path.write_text("{corrupted-layout", encoding="utf-8")
            raise OSError("simulated layout.json write failure")
        path.write_text(real_atomic_json(value, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(installation, "atomic_json", fail_only_layout_json)
    tty_ws_fail = FakeTerminal([str(tmp_path / "FailedWorkspace"), "", "yes"])
    with pytest.raises(OSError, match="simulated layout.json write failure"):
        wizard.run_wizard(layout, tty=tty_ws_fail, section="workspace")

    assert layout.config.read_bytes() == before_bytes
    assert layout_marker.read_bytes() == before_layout_bytes
    assert not list(layout.install.glob("layout.json.bak.*"))


def test_unsupported_section_and_no_tty_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = Layout.for_home(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)

    with pytest.raises(
        InstallError,
        match="Unsupported setup section 'invalid'; valid sections: workspace, model, converse",
    ):
        wizard_navigation.validate_setup_section("invalid")

    parser = launcher.build_parser()
    bad_args = parser.parse_args(["setup", "bogus"])
    with pytest.raises(
        InstallError,
        match="Unsupported setup section 'bogus'; valid sections: workspace, model, converse",
    ):
        cli.dispatch(bad_args)

    # No-TTY call to `kinaiya setup model` without `--yes` fails closed immediately
    @contextmanager
    def no_tty():
        raise OSError("No controlling terminal")
        yield

    monkeypatch.setattr(wizard, "open_terminal", no_tty)
    model_args = parser.parse_args(["setup", "model"])
    with pytest.raises(InstallError, match="No controlling terminal; use setup --yes"):
        cli.dispatch(model_args)
    assert not layout.config.exists()


class InterruptingTerminal(FakeTerminal):
    """Fake terminal where an answer entry may be an exception instance or class."""

    def readline(self) -> str:
        try:
            item = next(self._answers)
        except StopIteration:
            return ""
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, type) and issubclass(item, BaseException):
            raise item()
        return str(item) + "\n"


def test_keyboard_interrupt_cancels_cleanly_across_wizard_prompts_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    layout = Layout.for_home(tmp_path)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)

    # 1. Ctrl-C at raw prompt (e.g. Agent provider endpoint)
    tty_raw = InterruptingTerminal(["", "", KeyboardInterrupt()])
    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_raw)
    assert not layout.config.exists()

    # 2. Ctrl-C at y/n prompt (e.g. cloud disclosure)
    tty_yn = InterruptingTerminal(
        ["", "", "http://127.0.0.1:1234", "qwen2.5-coder", KeyboardInterrupt()]
    )
    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_yn)
    assert not layout.config.exists()

    # 3. Ctrl-C at final confirmation prompt
    tty_confirm = InterruptingTerminal(
        ["", "", "http://127.0.0.1:1234", "qwen2.5-coder", "no", "", "no", KeyboardInterrupt()]
    )
    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_confirm)
    assert not layout.config.exists()

    # 4. Ctrl-C in quick setup flow (ask and ask_yn)
    tty_quick_ask = InterruptingTerminal(["http://127.0.0.1:1234", KeyboardInterrupt()])
    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_quick_ask, mode="quick")
    assert not layout.config.exists()

    tty_quick_yn = InterruptingTerminal(
        ["http://127.0.0.1:1234", "qwen2.5-coder", KeyboardInterrupt()]
    )
    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_quick_yn, mode="quick")
    assert not layout.config.exists()

    # 5. Ctrl-C in masked API key prompt (readline and getpass_fn)
    from services.distribution.credentials import prompt_masked_api_key

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        prompt_masked_api_key(InterruptingTerminal([KeyboardInterrupt()]))

    def raising_getpass(_prompt: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        prompt_masked_api_key(FakeTerminal([]), getpass_fn=raising_getpass)

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        prompt_masked_api_key(InterruptingTerminal([":env", KeyboardInterrupt()]))

    # 6. Ctrl-C in interactive model picker and profile picker
    from services.distribution.model_picker import (
        ProviderProfileChoice,
        select_model_interactively,
        select_profile_interactively,
    )

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        select_model_interactively(
            InterruptingTerminal([KeyboardInterrupt()]),
            ["qwen2.5-coder-7b", "qwen2.5-coder-14b"],
        )

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        select_profile_interactively(
            InterruptingTerminal([KeyboardInterrupt()]),
            [
                ProviderProfileChoice(
                    profile_id="p01",
                    name="Local",
                    base_url="http://127.0.0.1:1234/v1",
                    adapter_kind="lmstudio",
                )
            ],
        )

    # 7. Ctrl-C in connection check: raises before save, skips cleanly after save
    from services.distribution.connection_probe import run_interactive_connection_check

    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        run_interactive_connection_check(
            InterruptingTerminal([KeyboardInterrupt()]),
            endpoint="http://127.0.0.1:1234/v1",
            model="qwen2.5-coder",
            config_saved=False,
        )

    probe_after_save = run_interactive_connection_check(
        InterruptingTerminal([KeyboardInterrupt()]),
        endpoint="http://127.0.0.1:1234/v1",
        model="qwen2.5-coder",
        config_saved=True,
    )
    assert probe_after_save.overall_readiness == "unverified_saved"

    # 8. End-to-end launcher.main(["setup"]) exits with code 1 and clean message, no traceback
    @contextmanager
    def ctrl_c_tty():
        yield InterruptingTerminal([KeyboardInterrupt()])

    monkeypatch.setattr(wizard, "open_terminal", ctrl_c_tty)
    with pytest.raises(SystemExit) as exc_info:
        launcher.main(["setup"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == "kinaiya: Setup cancelled; no configuration written\n"
    assert "Traceback" not in captured.err
    assert "KeyboardInterrupt" not in captured.err
    assert not layout.config.exists()

    # 9. Ctrl-C at optional post-save custom API profile prompt preserves written config
    tty_post_save = InterruptingTerminal(
        [
            "",
            "",
            "http://127.0.0.1:1234",
            "qwen2.5-coder",
            "no",
            "",
            "no",
            "yes",
            KeyboardInterrupt(),
        ]
    )
    res = wizard.run_wizard(layout, tty=tty_post_save)
    assert res["ready"] is False
    assert layout.config.exists()
    assert (
        "Cancelled; no profile was saved. Setup configuration is unchanged." in tty_post_save.output
    )
