"""#619 and #566: official planning observes the Core saved-mode source."""

import json
from types import SimpleNamespace

import pytest

from services.common.topology import SUBSYSTEM_MODES, SubsystemTopology
from services.distribution import doctor, installation


@pytest.mark.parametrize("converse,decisions", [(False, False), (False, True), (True, True)])
def test_doctor_top_level_schema_does_not_require_an_unselected_agent(
    tmp_path,
    monkeypatch,
    converse,
    decisions,
):
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    arguments = {"agent": False, "converse": converse, "decisions": decisions}
    for role, selected in (("converse", converse), ("decisions", decisions)):
        if selected:
            arguments[f"{role}_profile"] = {
                "kind": "compatible",
                "endpoint": "http://127.0.0.1:1234",
                "model": f"synthetic-{role}",
            }
    installation.setup(layout, **arguments)
    before = layout.config.read_bytes()
    assert "agent" not in json.loads(before)["providers"]
    result, config = doctor._check_layout_and_config(layout)
    assert result["status"] == "pass" and result["config_state"] == "valid"
    assert config["distribution"]["agent"] is False
    assert layout.config.read_bytes() == before


@pytest.fixture
def existing_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="synthetic-agent")
    return layout


@pytest.mark.parametrize("mode", SUBSYSTEM_MODES)
def test_managed_launch_captures_the_saved_mode_for_every_original_child(
    existing_layout, monkeypatch, mode
):
    from services import launcher

    layout = existing_layout
    selected = SubsystemTopology.from_mode(mode)
    before = layout.config.read_bytes()
    installation.reconfigure_section(
        layout,
        "services",
        agent=selected.agent,
        converse=selected.converse,
        decisions=selected.decisions,
    )
    commands = []
    monkeypatch.setattr(launcher, "supervise", lambda value: commands.append(value))
    args = SimpleNamespace(
        service="host",
        config=layout.config,
        data_dir=layout.data,
        workspace=layout.workspace,
        start_paused=False,
        converse_memory=False,
    )
    launcher.serve(args)
    assert len(commands) == 1 and set(commands[0]) == set(selected.services())
    for argv in commands[0].values():
        assert argv[argv.index("--startup-subsystem-mode") + 1] == mode
    assert layout.config.read_bytes() == before


def test_corrupt_saved_selection_refuses_before_token_or_process_entry(
    existing_layout, monkeypatch
):
    from services import launcher

    layout = existing_layout
    path = layout.data / "core-subsystems.sqlite"
    path.write_bytes(b"not-a-database")
    path.chmod(0o600)
    entered = []
    monkeypatch.setattr(launcher, "load_or_create_tokens", lambda *_: entered.append("tokens"))
    monkeypatch.setattr(launcher, "supervise", lambda *_: entered.append("processes"))
    args = SimpleNamespace(
        service="host",
        config=layout.config,
        data_dir=layout.data,
        workspace=layout.workspace,
        start_paused=False,
    )
    with pytest.raises((ValueError, RuntimeError, SystemExit)):
        launcher.serve(args)
    assert entered == [] and path.read_bytes() == b"not-a-database"
