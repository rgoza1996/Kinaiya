"""Explicit graphs and legacy re-setup keep selection separate from declarations."""

import json

import pytest

from services.common.topology import SubsystemTopology
from services.distribution import installation

MODES = [
    (False, False, False),
    (True, False, False),
    (False, False, True),
    (True, False, True),
    (False, True, True),
    (True, True, True),
]


@pytest.mark.parametrize("agent,converse,decisions", MODES)
def test_explicit_selection_does_not_activate_unselected_declared_roles(
    tmp_path, monkeypatch, agent, converse, decisions
):
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(
        layout,
        agent=agent,
        converse=converse,
        decisions=decisions,
        roles={
            role: {
                "kind": "lmstudio" if role == "agent" else "compatible",
                "endpoint": "http://127.0.0.1:1234",
                "model": f"synthetic-{role}",
            }
            for role in ("agent", "converse", "decisions")
        },
    )
    config = json.loads(layout.config.read_text())
    assert SubsystemTopology.from_config(config) == SubsystemTopology(agent, converse, decisions)
    for role in ("agent", "converse", "decisions"):
        assert config["providers"][role]["model"] == f"synthetic-{role}"


@pytest.mark.parametrize("old_mode", MODES)
@pytest.mark.parametrize("new_mode", MODES)
def test_mode_only_transition_preserves_saved_role_declarations(
    tmp_path, monkeypatch, old_mode, new_mode
):
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(
        layout, endpoint="http://127.0.0.1:1234", model="synthetic-agent", converse=True
    )
    original = json.loads(layout.config.read_text())
    original["distribution"] = dict(zip(("agent", "converse", "decisions"), old_mode, strict=True))
    original["distribution"]["saved_extension"] = ["keep"]
    layout.config.write_text(json.dumps(original))
    tokens = (layout.data / "service-tokens.json").read_bytes()
    installation.reconfigure_section(
        layout, "services", **dict(zip(("agent", "converse", "decisions"), new_mode, strict=True))
    )
    saved = json.loads(layout.config.read_text())
    assert SubsystemTopology.from_config(saved) == SubsystemTopology(*new_mode)
    assert saved["providers"] == original["providers"]
    assert saved["distribution"]["saved_extension"] == ["keep"]
    assert (layout.data / "service-tokens.json").read_bytes() == tokens


@pytest.mark.parametrize("old_mode", MODES)
def test_legacy_re_setup_resets_previous_explicit_process_selection(
    tmp_path, monkeypatch, old_mode
):
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(
        layout, endpoint="http://127.0.0.1:1234", model="synthetic-agent", converse=True
    )
    original = json.loads(layout.config.read_text())
    original["distribution"] = dict(zip(("agent", "converse", "decisions"), old_mode, strict=True))
    original["distribution"]["saved_extension"] = ["keep"]
    layout.config.write_text(json.dumps(original))
    installation.setup(
        layout, endpoint="http://127.0.0.1:1234", model="synthetic-new-agent", allow_existing=True
    )
    saved = json.loads(layout.config.read_text())
    assert SubsystemTopology.from_config(saved) == SubsystemTopology(True, False, False)
    assert saved["distribution"]["saved_extension"] == ["keep"]


def test_full_re_setup_with_agent_off_retains_its_saved_model(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="synthetic-retained-agent")
    saved_agent = json.loads(layout.config.read_text())["providers"]["agent"]
    installation.setup(
        layout,
        agent=False,
        decisions=True,
        allow_existing=True,
        decisions_profile={
            "kind": "compatible",
            "endpoint": "http://127.0.0.1:1235",
            "model": "synthetic-decisions",
        },
    )
    saved = json.loads(layout.config.read_text())
    assert saved["providers"]["agent"] == saved_agent
    assert SubsystemTopology.from_config(saved) == SubsystemTopology(False, False, True)
