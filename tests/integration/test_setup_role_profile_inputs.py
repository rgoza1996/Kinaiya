"""Source-aware main62 setup inputs; future repair/Muse source was unread.

These exercise the real wizard and launcher parser/dispatch/setup/init, not live
providers. Layout, terminal IO, host probes and provider construction are fixtures.
The provider stand-in proves no readiness, measurement or model interoperability.
"""

import io
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import launcher
from services.distribution import cli, installation, wizard

_AGENT_REF = "KINAIYA_TEST_335_AGENT_CREDENTIAL"
_CONVERSE_REF = "KINAIYA_TEST_335_CONVERSE_CREDENTIAL"
_DECISIONS_REF = "KINAIYA_TEST_335_DECISIONS_CREDENTIAL"
_LOCAL_AGENT = "http://127.0.0.1:1234"
_LOCAL_DECISIONS = "http://127.0.0.1:8080/v1"


def _role(adapter, endpoint, model, *, cloud=False, credential=None):
    return {
        "adapter": adapter,
        "endpoint": endpoint,
        "model": model,
        "cloud": cloud,
        "credential": credential,
    }


_VECTORS = (
    {
        "agent": _role("lmstudio", _LOCAL_AGENT, "local-agent"),
        "converse": _role(
            "compatible",
            "https://converse-a.example.invalid/v1",
            "cloud-converse",
            cloud=True,
            credential=_CONVERSE_REF,
        ),
        "decisions": _role("compatible", _LOCAL_DECISIONS, "local-decisions"),
    },
    {
        "agent": _role(
            "meta",
            "https://agent.example.invalid/v1",
            "cloud-agent",
            cloud=True,
            credential=_AGENT_REF,
        ),
        "converse": _role("lmstudio", _LOCAL_AGENT, "local-converse"),
        "decisions": _role("compatible", _LOCAL_DECISIONS, "local-decisions"),
    },
    {
        "agent": _role("lmstudio", _LOCAL_AGENT, "local-agent"),
        "converse": _role(
            "meta",
            "https://converse-b.example.invalid/v1",
            "cloud-converse",
            cloud=True,
            credential=_CONVERSE_REF,
        ),
        "decisions": _role(
            "meta",
            "https://decisions.example.invalid/v1",
            "cloud-decisions",
            cloud=True,
            credential=_DECISIONS_REF,
        ),
    },
)


class _TTY(io.StringIO):
    """Answer actual prompts by role/field; don't freeze their display ordering."""

    def __init__(self, vector, *, converse=True):
        super().__init__()
        self.vector = vector
        self.converse = converse
        self.role = "agent"
        self.prompt = ""
        self.questions = []

    def write(self, text):
        self.prompt = text.casefold()
        return super().write(text)

    def readline(self, *args, **kwargs):
        prompt = self.prompt
        if prompt == (
            "provider api key for p01 (enter to skip, paste masked key, or :env var; "
            "saved to 0600 plaintext .env, not a vault): "
        ):
            self.role = "agent"
            self.questions.append(("agent", "credential"))
            credential = self.vector["agent"]["credential"]
            return (":env " + credential if credential else "") + "\n"
        if "authorized workspace" in prompt or "private data directory" in prompt:
            return "\n"
        if "enable" in prompt and "converse" in prompt:
            if self.converse:
                self.role = "converse"
            return "yes\n" if self.converse else "\n"
        if "write" in prompt and ("configuration" in prompt or "tokens" in prompt):
            return "yes\n"
        for role in ("agent", "converse", "decisions"):
            if role in prompt:
                self.role = role
        values = self.vector[self.role]
        if "credential" in prompt:
            field, answer = "credential", values["credential"] or ""
        elif "disclosure" in prompt or ("allow" in prompt and "cloud" in prompt):
            field, answer = "cloud", "yes" if values["cloud"] else ""
        elif "endpoint" in prompt:
            field, answer = "endpoint", values["endpoint"]
            if self.role == "agent":
                answer = values["adapter"] + "=" + answer
        elif "adapter" in prompt:
            field, answer = "adapter", values["adapter"]
        elif "model" in prompt:
            field, answer = "model", values["model"]
        else:
            raise AssertionError("Unexpected setup prompt: " + prompt)
        self.questions.append((self.role, field))
        return answer + "\n"


@pytest.fixture
def native_setup(monkeypatch, tmp_path):
    home = tmp_path / "fresh-home"
    layout = installation.Layout.for_home(home)
    effects = []
    mkdir = Path.mkdir
    write_text = Path.write_text
    initialize = launcher.initialize
    tokens = launcher.load_or_create_tokens

    def in_scope(path):
        return path == home or home in path.parents

    def observed_mkdir(path, *args, **kwargs):
        if in_scope(path):
            effects.append("mkdir")
        return mkdir(path, *args, **kwargs)

    def observed_write(path, *args, **kwargs):
        if in_scope(path):
            effects.append("write")
        return write_text(path, *args, **kwargs)

    def observed_initialize(args):
        effects.append("initialize")
        return initialize(args)

    def observed_tokens(path):
        effects.append("tokens")
        return tokens(path)

    def no_inference(*args, **kwargs):
        raise AssertionError("Setup must not enter model/provider work")

    def declaration_only_provider(config, role, **kwargs):
        # The real validate_setup_provider and endpoint/credential-name checks
        # run; this boundary prevents constructor/environment/provider work.
        return SimpleNamespace(
            prepare_complete=no_inference,
            cloud_prompt_measurement=config["providers"][role]["kind"] == "meta",
        )

    monkeypatch.setattr(cli, "chosen_layout", lambda: layout)
    monkeypatch.setattr(installation, "check_outdated_system_blockers", lambda **kwargs: None)
    monkeypatch.setattr(wizard, "check_outdated_system_blockers", lambda **kwargs: None)
    monkeypatch.setattr(launcher, "provider", declaration_only_provider)
    monkeypatch.setattr(launcher, "initialize", observed_initialize)
    monkeypatch.setattr(launcher, "load_or_create_tokens", observed_tokens)
    monkeypatch.setattr(Path, "mkdir", observed_mkdir)
    monkeypatch.setattr(Path, "write_text", observed_write)
    for name in (_AGENT_REF, _CONVERSE_REF, _DECISIONS_REF):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(home=home, layout=layout, effects=effects)


def _base_args():
    return ["setup", "--yes", "--endpoint", _LOCAL_AGENT, "--model", "local-agent"]


def _profile_args(role, profile):
    prefix = "--" if role == "agent" else "--" + role + "-"
    result = [
        prefix + "adapter",
        profile["adapter"],
        prefix + "endpoint",
        profile["endpoint"],
        prefix + "model",
        profile["model"],
    ]
    if profile["cloud"]:
        result.append(prefix + "allow-cloud")
    if profile["credential"] is not None:
        result.extend((prefix + "credential-env", profile["credential"]))
    return result


def _saved(env):
    return json.loads(env.layout.config.read_text())


def _assert_profile(actual, expected):
    assert actual["kind"] == expected["adapter"]
    assert actual["base_url"] == expected["endpoint"]
    assert actual["model"] == expected["model"]
    assert actual.get("allow_cloud", False) is expected["cloud"]
    assert actual.get("api_key_env") == expected["credential"]
    assert "api_key" not in actual and "secret_ref" not in actual


def _assert_not_ready(result):
    assert result["ready"] is False
    assert result["provider_connection"] == "not_checked"


def _assert_agent_defaults(profile):
    if profile["kind"] == "lmstudio":
        assert profile["max_tokens"] == 128
        assert profile["reasoning_effort"] == "none"
    else:
        assert profile["kind"] == "meta"
        assert profile["max_tokens"] == 2048 and profile["context_tokens"] == 32768
        assert profile["structured_actions"] is True
        assert profile["action_format"] == "flat-v1"


def _cli_success(args, capsys):
    launcher.main(args)
    result = json.loads(capsys.readouterr().out)
    _assert_not_ready(result)
    return result


def _tty_success(monkeypatch, tty, capsys):
    @contextmanager
    def terminal():
        yield tty

    monkeypatch.setattr(wizard, "open_terminal", terminal)
    return _cli_success(["setup"], capsys)


def _cli_refused(env, args, capsys, *, exit_code=1):
    with pytest.raises(SystemExit) as error:
        launcher.main(args)
    assert error.value.code == exit_code
    captured = capsys.readouterr()
    assert not env.home.exists()
    assert env.effects == [], "Validation must precede the first attempted setup effect"
    return captured


@pytest.mark.parametrize(
    "vector",
    _VECTORS,
    ids=("local-agent-cloud-converse", "cloud-agent-local-converse", "distinct-cloud-refs"),
)
def test_real_tty_collects_independent_role_cloud_and_variable_names(
    native_setup, vector, monkeypatch, capsys
):
    tty = _TTY(vector)
    result = _tty_success(monkeypatch, tty, capsys)
    _assert_not_ready(result)
    config = _saved(native_setup)
    for role in ("agent", "converse", "decisions"):
        _assert_profile(config["providers"][role], vector[role])
        assert (role, "cloud") in tty.questions
        assert (role, "credential") in tty.questions
    _assert_agent_defaults(config["providers"]["agent"])
    assert config["distribution"]["converse"] is True
    assert "Provider connection is not checked" in tty.getvalue()
    assert "initialize" in native_setup.effects and "tokens" in native_setup.effects


def test_real_tty_empty_optional_choice_preserves_agent_only_default(
    native_setup, monkeypatch, capsys
):
    tty = _TTY(_VECTORS[0], converse=False)
    _assert_not_ready(_tty_success(monkeypatch, tty, capsys))
    config = _saved(native_setup)
    assert config["distribution"]["converse"] is False
    _assert_profile(config["providers"]["agent"], _VECTORS[0]["agent"])
    assert not any(role in {"converse", "decisions"} for role, field in tty.questions)


@pytest.mark.parametrize(
    "vector",
    _VECTORS,
    ids=("local-agent-cloud-converse", "cloud-agent-local-converse", "distinct-cloud-refs"),
)
def test_top_level_cli_parses_dispatches_and_saves_independent_profiles(
    native_setup, vector, capsys
):
    args = ["setup", "--yes"]
    for role in ("agent", "converse", "decisions"):
        args.extend(_profile_args(role, vector[role]))
    _cli_success(args, capsys)
    config = _saved(native_setup)
    for role in ("agent", "converse", "decisions"):
        _assert_profile(config["providers"][role], vector[role])
    _assert_agent_defaults(config["providers"]["agent"])
    assert config["distribution"]["converse"] is True


@pytest.mark.parametrize("role", ("converse", "decisions"))
def test_explicit_role_enablement_does_not_enable_unrequested_converse(native_setup, role, capsys):
    profile = _role("compatible", _LOCAL_DECISIONS, "selected-" + role)
    # Endpoint+model is a complete explicit role; its adapter remains optional.
    _cli_success(
        _base_args()
        + [
            "--" + role + "-endpoint",
            profile["endpoint"],
            "--" + role + "-model",
            profile["model"],
        ],
        capsys,
    )
    config = _saved(native_setup)
    _assert_profile(config["providers"][role], profile)
    assert config["distribution"]["converse"] is (role == "converse")


@pytest.mark.parametrize("adapter", ("laya", "native_d1"))
def test_top_level_cli_retains_supported_local_decisions_adapters(native_setup, adapter, capsys):
    profile = _role(
        adapter,
        "http://127.0.0.1:8080" if adapter == "native_d1" else _LOCAL_DECISIONS,
        "typed-decisions",
    )
    _cli_success(_base_args() + _profile_args("decisions", profile), capsys)
    config = _saved(native_setup)
    _assert_profile(config["providers"]["decisions"], profile)
    assert config["distribution"]["converse"] is False


def test_old_converse_flag_preserves_measured_agent_and_implicit_profile_defaults(
    native_setup, capsys
):
    _cli_success(_base_args() + ["--converse"], capsys)
    config = _saved(native_setup)
    agent = config["providers"]["agent"]
    assert agent["kind"] == "lmstudio" and agent["max_tokens"] == 128
    assert agent["reasoning_effort"] == "none" and agent["allow_cloud"] is False
    assert "api_key_env" not in agent
    for role, adapter in (("converse", "lmstudio"), ("decisions", "compatible")):
        _assert_profile(config["providers"][role], _role(adapter, _LOCAL_AGENT, "local-agent"))
    assert config["distribution"]["converse"] is True


def test_implicit_decisions_rejection_is_pre_effect_and_explicit_retry_works(native_setup, capsys):
    args = [
        "setup",
        "--yes",
        "--endpoint",
        "ws://127.0.0.1:1234",
        "--model",
        "local-agent",
        "--converse",
    ]
    _cli_refused(native_setup, args, capsys)
    corrected = _role("compatible", _LOCAL_DECISIONS, "http-decisions")
    _cli_success(args + _profile_args("decisions", corrected), capsys)
    config = _saved(native_setup)
    assert config["providers"]["agent"]["base_url"] == "ws://127.0.0.1:1234"
    assert config["providers"]["converse"]["base_url"] == "ws://127.0.0.1:1234"
    _assert_profile(config["providers"]["decisions"], corrected)
    assert "initialize" in native_setup.effects and "tokens" in native_setup.effects


@pytest.mark.parametrize("role", ("converse", "decisions"))
@pytest.mark.parametrize(
    "field,value",
    (
        ("adapter", "compatible"),
        ("endpoint", _LOCAL_DECISIONS),
        ("model", "selected"),
        ("credential-env", _CONVERSE_REF),
        ("allow-cloud", None),
    ),
)
def test_any_explicit_role_field_requires_both_endpoint_and_model_before_effects(
    native_setup, role, field, value, capsys
):
    extra = ["--" + role + "-" + field]
    if value is not None:
        extra.append(value)
    _cli_refused(native_setup, _base_args() + extra, capsys)


@pytest.mark.parametrize("role", ("agent", "converse", "decisions"))
def test_cli_credential_input_is_variable_name_not_a_raw_value(native_setup, role, capsys):
    invalid = "synthetic-value-not-an-environment-name"
    profile = _role(
        "meta" if role == "agent" else "compatible",
        "https://selected.example.invalid/v1",
        "selected",
        cloud=True,
        credential=invalid,
    )
    args = (
        ["setup", "--yes"] + _profile_args(role, profile)
        if role == "agent"
        else _base_args() + _profile_args(role, profile)
    )
    captured = _cli_refused(native_setup, args, capsys)
    assert invalid not in captured.out + captured.err


def test_cli_does_not_enable_unmeasured_compatible_agent(native_setup, capsys):
    _cli_refused(native_setup, _base_args() + ["--adapter", "compatible"], capsys, exit_code=2)


@pytest.mark.parametrize("role", ("converse", "decisions"))
def test_role_cloud_permission_cannot_inherit_agent_cloud_grant(native_setup, role, capsys):
    agent = _VECTORS[1]["agent"]
    undisclosed = _role(
        "compatible",
        "https://separate-role.example.invalid/v1",
        "separate-role",
        credential=_CONVERSE_REF if role == "converse" else _DECISIONS_REF,
    )
    args = ["setup", "--yes"] + _profile_args("agent", agent) + _profile_args(role, undisclosed)
    _cli_refused(native_setup, args, capsys)


def test_existing_configuration_and_siblings_survive_refused_setup(native_setup, capsys):
    native_setup.layout.config.parent.mkdir(parents=True)
    original = b'{"existing":"synthetic-owner-configuration"}\n'
    native_setup.layout.config.write_bytes(original)
    sibling = native_setup.layout.config.parent / "keep.txt"
    sibling.write_bytes(b"preserve sibling\n")
    before = {
        p.relative_to(native_setup.home): (p.stat().st_mode, p.read_bytes())
        for p in native_setup.home.rglob("*")
        if p.is_file()
    }
    native_setup.effects.clear()
    with pytest.raises(SystemExit) as error:
        launcher.main(
            _base_args()
            + _profile_args("converse", _role("compatible", _LOCAL_DECISIONS, "selected"))
        )
    assert error.value.code == 1
    capsys.readouterr()
    assert native_setup.effects == []
    assert {
        p.relative_to(native_setup.home): (p.stat().st_mode, p.read_bytes())
        for p in native_setup.home.rglob("*")
        if p.is_file()
    } == before


@pytest.mark.parametrize("flag", ("--api-key", "--converse-api-key", "--decisions-api-key"))
def test_raw_credential_value_options_are_not_accepted(native_setup, flag, capsys):
    _cli_refused(
        native_setup, _base_args() + [flag, "synthetic-nonsecret-value"], capsys, exit_code=2
    )
