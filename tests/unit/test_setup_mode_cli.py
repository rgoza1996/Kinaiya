"""#566: explicit headless mode setup and non-destructive mode-only edits."""

import json

import pytest

from services import launcher
from services.common.topology import SubsystemTopology
from services.distribution import cli, installation
from services.settings.subsystems import effective_config

MODES = [
    ("core", False, False, False),
    ("agent", True, False, False),
    ("decisions", False, False, True),
    ("agent-decisions", True, False, True),
    ("converse-decisions", False, True, True),
    ("all", True, True, True),
]


@pytest.fixture
def layout(tmp_path, monkeypatch):
    layout = installation.Layout.for_home(tmp_path)
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)
    return layout


def dispatch(arguments):
    return cli.dispatch(launcher.build_parser().parse_args(arguments))


@pytest.mark.parametrize("mode,agent,converse,decisions", MODES)
def test_explicit_cli_mode_configures_only_selected_roles(layout, mode, agent, converse, decisions):
    arguments = ["setup", "--yes", "--services", mode]
    if agent:
        arguments += ["--endpoint", "http://127.0.0.1:1234", "--model", "synthetic-agent"]
    for role, selected in (("converse", converse), ("decisions", decisions)):
        if selected:
            arguments += [
                f"--{role}-endpoint",
                "http://127.0.0.1:1235",
                f"--{role}-model",
                f"synthetic-{role}",
            ]
    result = dispatch(arguments)
    saved = json.loads(layout.config.read_text())
    assert SubsystemTopology.from_config(saved) == SubsystemTopology(agent, converse, decisions)
    assert result["subsystems"] == {"agent": agent, "converse": converse, "decisions": decisions}
    assert result["ready"] is False and result["provider_connection"] == "not_checked"
    if not agent:
        assert "agent" not in saved["providers"]


@pytest.mark.parametrize("mode,agent,converse,decisions", MODES)
def test_headless_mode_only_edit_needs_no_provider_arguments_or_token_rotation(
    layout, mode, agent, converse, decisions
):
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="synthetic-retained")
    original = json.loads(layout.config.read_text())
    original_bytes = layout.config.read_bytes()
    tokens = (layout.data / "service-tokens.json").read_bytes()
    receipt = dispatch(["setup", "services", "--yes", "--services", mode])
    assert layout.config.read_bytes() == original_bytes
    saved = effective_config(json.loads(layout.config.read_text()), layout.data, layout.workspace)
    assert SubsystemTopology.from_config(saved) == SubsystemTopology(agent, converse, decisions)
    assert saved["providers"] == original["providers"]
    assert (layout.data / "service-tokens.json").read_bytes() == tokens
    assert receipt["activation"] == "restart_required" and receipt["ready"] is False


@pytest.mark.parametrize(
    "arguments,error",
    [
        (["setup", "--services", "decisions"], "requires --yes"),
        (["setup", "services", "--yes"], "requires --services"),
        (["setup", "model", "--yes", "--services", "core"], "full setup or the services section"),
        (["setup", "--yes", "--services", "core", "--converse"], "Conflicting"),
        (
            ["setup", "services", "--yes", "--services", "core", "--model", "ignored"],
            "Mode-only edits",
        ),
        (["setup", "--yes", "--services", "decisions"], "Decisions mode requires"),
        (["setup", "--yes", "--services", "converse-decisions"], "Converse mode requires"),
    ],
)
def test_incomplete_or_conflicting_explicit_cli_modes_refuse_without_files(
    layout, arguments, error
):
    with pytest.raises(installation.InstallError, match=error):
        dispatch(arguments)
    assert not layout.config.exists() and not layout.data.exists()


def test_invalid_cli_mode_never_reaches_configuration(layout):
    with pytest.raises(SystemExit) as error:
        launcher.build_parser().parse_args(
            ["setup", "--yes", "--services", "converse-without-decisions"]
        )
    assert error.value.code == 2
    assert not layout.config.exists() and not layout.data.exists()
