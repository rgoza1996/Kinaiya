"""#566: select services before configuring only their owned providers."""

import json

import pytest

from services.common.topology import SubsystemTopology
from services.distribution import installation, wizard_navigation
from services.distribution.credentials import MaskedKeyAction

MODES = [
    ("3", True, False, False),
    ("5", False, False, True),
    ("6", True, False, True),
    ("4", False, True, True),
    ("2", True, True, True),
    ("7", False, False, False),
]


class Terminal:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.output = ""

    def write(self, value):
        self.output += value

    def flush(self):
        pass

    def readline(self):
        answer = next(self.answers, None)
        return "" if answer is None else answer + "\n"


@pytest.fixture(autouse=True)
def no_host_prerequisite_probe(monkeypatch):
    # Real native initialization and saves; isolated layouts, no package work.
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **_: None)


def navigator(layout, terminal, **kwargs):
    return wizard_navigation.WizardNavigator(
        layout,
        tty=terminal,
        setup_fn=installation.setup,
        reconfigure_fn=installation.reconfigure_section,
        **kwargs,
    )


@pytest.mark.parametrize("choice,agent,converse,decisions", MODES)
def test_first_run_selects_mode_before_only_selected_role_prompts(
    tmp_path,
    choice,
    agent,
    converse,
    decisions,
):
    layout = installation.Layout.for_home(tmp_path)
    answers = ["", "", choice]
    if agent:
        answers += ["http://127.0.0.1:1234", "synthetic-agent", "no", ""]
    for role, enabled, port in (("converse", converse, 1235), ("decisions", decisions, 1236)):
        if enabled:
            answers += ["compatible", f"http://127.0.0.1:{port}", f"synthetic-{role}", "no", ""]
    answers += ["yes"]
    terminal = Terminal(answers)
    receipt = navigator(layout, terminal).run()
    config = json.loads(layout.config.read_text())
    assert SubsystemTopology.from_config(config) == SubsystemTopology(agent, converse, decisions)
    assert receipt["ready"] is False
    assert receipt["subsystems"] == {"agent": agent, "converse": converse, "decisions": decisions}
    if not agent:
        assert "Agent provider endpoint" not in terminal.output
        assert "Credential environment variable NAME only" not in terminal.output
        assert "Agent: Disabled" in terminal.output
        assert "agent" not in config["providers"]
    for role, enabled in (("agent", agent), ("converse", converse), ("decisions", decisions)):
        if enabled:
            assert config["providers"][role]["model"] == f"synthetic-{role}"


@pytest.mark.parametrize("choice,agent,converse,decisions", MODES)
def test_mode_only_edit_preserves_models_credentials_and_unrelated_settings(
    tmp_path,
    choice,
    agent,
    converse,
    decisions,
):
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="synthetic-original")
    original = json.loads(layout.config.read_text())
    original["distribution"]["keep_distribution_setting"] = {"opaque": [1, 2]}
    original["keep_unrelated_setting"] = {"nested": ["original"]}
    layout.config.write_text(json.dumps(original))
    tokens = (layout.data / "service-tokens.json").read_bytes()
    credentials = layout.config.parent / "providers.env"
    credentials.write_bytes(b"# private synthetic credential fixture\n")
    receipt = navigator(layout, Terminal([choice, "yes"]), section="services").run()
    saved = json.loads(layout.config.read_text())
    expected = {"agent": agent, "converse": converse, "decisions": decisions}
    assert {k: saved["distribution"][k] for k in expected} == expected
    original["distribution"].update(expected)
    assert saved == original
    assert (layout.data / "service-tokens.json").read_bytes() == tokens
    assert credentials.read_bytes() == b"# private synthetic credential fixture\n"
    assert receipt["subsystems"] == expected
    assert receipt["activation"] == "restart_required"
    assert receipt["ready"] is False


@pytest.mark.parametrize("bad", [None, "yes", 0, 1, [], {}])
def test_explicit_invalid_selection_refuses_before_any_setup_write(tmp_path, bad):
    layout = installation.Layout.for_home(tmp_path)
    with pytest.raises(installation.InstallError, match="Subsystem selections must be booleans"):
        installation.setup(layout, agent=bad, converse=False, decisions=True)
    assert not layout.config.exists() and not layout.data.exists()


@pytest.mark.parametrize("agent", [False, True])
def test_explicit_converse_without_decisions_refuses_before_setup_write(tmp_path, agent):
    layout = installation.Layout.for_home(tmp_path)
    with pytest.raises(installation.InstallError, match="Converse requires Decisions"):
        installation.setup(layout, agent=agent, converse=True, decisions=False)
    assert not layout.config.exists() and not layout.data.exists()


def test_disabled_agent_does_not_apply_old_draft_key_or_probe_agent(tmp_path, monkeypatch):
    layout = installation.Layout.for_home(tmp_path)
    nav = navigator(layout, Terminal([]), check_connection=True)
    nav.answers.update(
        converse="converse_only",
        converse_model="synthetic-converse",
        decisions_model="synthetic-decisions",
    )
    nav.key_action = MaskedKeyAction(
        action="remove", profile_id="p01", endpoint="http://127.0.0.1:1234", credential_env=None
    )
    credentials = wizard_navigation.default_providers_env_path(layout)
    credentials.parent.mkdir(parents=True)
    credentials.write_bytes(b"# keep existing synthetic provider credentials\n")
    monkeypatch.setattr(
        wizard_navigation,
        "apply_masked_key_action",
        lambda *a, **k: pytest.fail("disabled Agent changed credentials"),
    )
    from services.distribution import connection_probe

    monkeypatch.setattr(
        connection_probe,
        "run_interactive_connection_check",
        lambda *a, **k: pytest.fail("disabled Agent was probed"),
    )
    result = nav._commit(layout)
    assert result["ready"] is False
    assert credentials.read_bytes() == b"# keep existing synthetic provider credentials\n"


def test_changing_agent_draft_preserves_explicit_other_role_models_and_endpoints(tmp_path):
    nav = navigator(installation.Layout.for_home(tmp_path), Terminal([]))
    nav.answers.update(
        endpoint="http://127.0.0.1:1234",
        model="old-agent",
        converse_endpoint="http://127.0.0.1:1235",
        converse_model="independent-converse",
        decisions_endpoint="http://127.0.0.1:1236",
        decisions_model="independent-decisions",
    )
    original = dict(nav.answers)
    nav._record_answer("endpoint", "http://127.0.0.1:1237")
    nav._record_answer("model", "new-agent")
    for key in ("converse_endpoint", "converse_model", "decisions_endpoint", "decisions_model"):
        assert nav.answers[key] == original[key]
        assert nav._default_for(key) == original[key]


@pytest.mark.parametrize("choice,agent,converse,decisions", MODES)
def test_mode_only_cancel_or_eof_preserves_exact_saved_bytes(
    tmp_path, choice, agent, converse, decisions
):
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(layout, endpoint="http://127.0.0.1:1234", model="synthetic-original")
    original = layout.config.read_bytes()
    for final in ("cancel", "no", None):
        answers = [choice] + ([] if final is None else [final])
        with pytest.raises(installation.InstallError, match="cancelled"):
            navigator(layout, Terminal(answers), section="services").run()
        assert layout.config.read_bytes() == original


def test_mode_only_edit_requires_existing_configuration_without_initialization(tmp_path):
    layout = installation.Layout.for_home(tmp_path)
    with pytest.raises(installation.InstallError, match="Existing configuration"):
        installation.reconfigure_section(
            layout, "services", agent=False, converse=False, decisions=True
        )
    assert not layout.config.exists() and not layout.data.exists()


@pytest.mark.parametrize("choice,agent", [("5", False), ("6", True)])
def test_existing_converse_section_can_configure_decisions_without_enabling_converse(
    tmp_path,
    choice,
    agent,
):
    layout = installation.Layout.for_home(tmp_path)
    installation.setup(
        layout, endpoint="http://127.0.0.1:1234", model="synthetic-retained-agent", converse=True
    )
    original = json.loads(layout.config.read_text())
    terminal = Terminal(
        [
            choice,
            "compatible",
            "http://127.0.0.1:1236",
            "synthetic-independent-decisions",
            "no",
            "",
            "yes",
        ]
    )
    navigator(layout, terminal, section="converse").run()
    saved = json.loads(layout.config.read_text())
    assert SubsystemTopology.from_config(saved) == SubsystemTopology(agent, False, True)
    assert saved["providers"]["agent"] == original["providers"]["agent"]
    assert saved["providers"]["converse"] == original["providers"]["converse"]
    assert saved["providers"]["decisions"]["model"] == "synthetic-independent-decisions"
    assert saved["providers"]["decisions"]["base_url"] == "http://127.0.0.1:1236"
