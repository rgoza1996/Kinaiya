"""Issue #381: boolean prompts on the active Quick/Advanced wizard coordinator."""

import pytest

from services.distribution import wizard, wizard_flow
from services.distribution.installation import InstallError, Layout


class Terminal:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.output = ""

    def write(self, text):
        self.output += text

    def flush(self):
        pass

    def readline(self):
        answer = next(self.answers, None)
        return "" if answer is None else answer + "\n"


@pytest.mark.parametrize(
    "answer,default,expected",
    [
        ("y", False, True),
        (" Y ", False, True),
        ("YeS", False, True),
        ("n", False, False),
        (" N ", False, False),
        ("nO", False, False),
        ("", False, False),
        ("", True, True),
    ],
)
def test_shared_boolean_prompt_accepts_short_and_long_answers(answer, default, expected):
    tty = Terminal(["maybe", answer])
    assert wizard.ask_yn is wizard_flow.ask_yn
    assert wizard.ask_yn(tty, "Continue?", default=default) is expected
    assert tty.output.count("Please enter y or n.") == 1
    hint = "y" if default else "n"
    assert tty.output.count(f"Continue? (y/n) [{hint}]: ") == 2


@pytest.mark.parametrize("default", [False, True])
def test_shared_boolean_prompt_refuses_eof_even_with_an_affirmative_default(default):
    with pytest.raises(InstallError, match="terminal input closed"):
        wizard.ask_yn(Terminal(["maybe"]), "Continue?", default=default)


@pytest.mark.parametrize("mode", ["quick", "standard", "advanced"])
def test_active_wizard_reprompts_boolean_answers_without_changing_default_permissions(
    tmp_path, mode
):
    layout = Layout.for_home(tmp_path)
    answers = (
        ["", "local-model", "maybe", "n", "maybe", "n", "maybe", "Y"]
        if mode == "quick"
        else ["", "", "", "local-model", "maybe", "n", "", "maybe", "n", "maybe", "Y"]
    )
    tty = Terminal(answers)
    calls = []
    coordinator = wizard_flow.WizardFlowCoordinator(
        layout,
        tty=tty,
        mode=mode,
        setup_fn=lambda chosen, **kwargs: calls.append((chosen, kwargs)) or {"ready": False},
    )
    result = coordinator.run()
    assert result["ready"] is False
    assert result["readiness_state"] == "configured_untested"
    assert len(calls) == 1
    assert calls[0][1]["allow_cloud"] is False
    assert calls[0][1]["converse"] is False
    assert calls[0][1]["model"] == "local-model"
    assert tty.output.count("Please enter y or n.") == 3
    assert "(y/n) [n]: " in tty.output
    assert "(y/n) [y]: " not in tty.output
    assert "yes/no" not in tty.output and "type yes" not in tty.output
    assert not layout.config.exists()
    assert not (layout.data / "service-tokens.json").exists()
    assert not (layout.data / "providers.env").exists()


@pytest.mark.parametrize("mode", ["quick", "standard", "advanced"])
def test_active_wizard_eof_after_invalid_consent_never_calls_setup(tmp_path, mode):
    layout = Layout.for_home(tmp_path)
    answers = (
        ["", "local-model", "n", "n", "maybe"]
        if mode == "quick"
        else ["", "", "", "local-model", "n", "", "n", "maybe"]
    )
    coordinator = wizard_flow.WizardFlowCoordinator(
        layout,
        tty=Terminal(answers),
        mode=mode,
        setup_fn=lambda *args, **kwargs: pytest.fail("EOF cannot authorize setup"),
    )
    with pytest.raises(InstallError, match="terminal input closed"):
        coordinator.run()
    assert not layout.config.exists()
    assert not layout.data.exists()


def test_each_optional_role_disclosure_reprompts_and_defaults_to_no(tmp_path):
    tty = Terminal(
        [
            "compatible",
            "https://converse.example.invalid/v1",
            "converse-model",
            "maybe",
            "",
            "",
            "compatible",
            "https://decisions.example.invalid/v1",
            "decisions-model",
            "maybe",
            "N",
            "",
        ]
    )
    coordinator = wizard_flow.WizardFlowCoordinator(Layout.for_home(tmp_path), tty=tty)
    converse, decisions = coordinator._collect_role_profiles(
        endpoint="http://127.0.0.1:1234", model="local-model"
    )
    assert converse["allow_cloud"] is False
    assert decisions["allow_cloud"] is False
    assert tty.output.count("Please enter y or n.") == 2
    assert "Converse provider? (y/n) [n]: " in tty.output
    assert "Decisions provider? (y/n) [n]: " in tty.output


@pytest.mark.parametrize("role_index", [0, 1])
def test_optional_role_disclosure_eof_never_accepts(tmp_path, role_index):
    first_role = ["compatible", "", "", "n", ""] if role_index else []
    tty = Terminal([*first_role, "compatible", "", "", "maybe"])
    coordinator = wizard_flow.WizardFlowCoordinator(Layout.for_home(tmp_path), tty=tty)
    with pytest.raises(InstallError, match="terminal input closed"):
        coordinator._collect_role_profiles(endpoint="http://127.0.0.1:1234", model="local-model")
