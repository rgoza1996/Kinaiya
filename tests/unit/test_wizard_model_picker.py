"""Issue #390: Setup wizard searchable provider/model choice using existing profiles."""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from services.distribution import wizard
from services.distribution.installation import InstallError, Layout
from services.distribution.model_picker import (
    MAX_DISCOVERED_MODELS,
    ProviderProfileChoice,
    check_local_v1_hint,
    check_role_adapter_eligibility,
    discover_profile_models,
    format_profile_choices,
    get_starter_provider_presets,
    load_role_model_selection,
    load_stored_profiles,
    save_role_model_selection,
    select_model_interactively,
    select_profile_interactively,
)


class _FakeTTY:
    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.output = ""

    def write(self, text: str) -> None:
        self.output += text

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        try:
            return next(self._answers) + "\n"
        except StopIteration:
            return ""


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._stream = io.BytesIO(body)
        self.status = status

    def read(self, amt: int | None = None) -> bytes:
        return self._stream.read(amt)

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def test_single_model_provider_selected_with_enter_without_retyping() -> None:
    tty = _FakeTTY([""])  # Blank Enter selects the sole model immediately
    selected = select_model_interactively(
        tty,
        ["qwen2.5-coder-14b-instruct"],
        profile_id="p01",
        role="agent",
        adapter_kind="lmstudio",
        endpoint="http://127.0.0.1:1234",
    )
    assert selected.identity == ("p01", "qwen2.5-coder-14b-instruct")
    assert selected.qualified_id == "p01:qwen2.5-coder-14b-instruct"
    assert selected.selection_source == "discovered"
    assert "Press Enter to select 'qwen2.5-coder-14b-instruct'" in tty.output


def test_multi_model_provider_filtered_via_search_query_and_selected_by_number() -> None:
    models = [
        "llama-3.1-8b-instruct",
        "mistral-nemo-12b",
        "qwen2.5-coder-7b",
        "qwen2.5-coder-32b",
        "deepseek-r1-distill-qwen-14b",
    ]
    # Filter with /qwen2.5-coder, then select item 2 from the filtered list (qwen2.5-coder-32b)
    tty = _FakeTTY(["/qwen2.5-coder", "2"])
    selected = select_model_interactively(
        tty,
        models,
        profile_id="p02",
        role="agent",
        adapter_kind="meta",
        endpoint="https://api.example.com/v1",
        credential_env="KINAIYA_PROVIDER_P02_API_KEY",
        allow_cloud=True,
    )
    assert selected.identity == ("p02", "qwen2.5-coder-32b")
    assert selected.qualified_id == "p02:qwen2.5-coder-32b"
    assert selected.selection_source == "discovered"
    assert "2 match for /qwen2.5-coder" in tty.output


def test_two_profiles_exposing_same_model_id_retain_distinct_identity_across_save_and_reload(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "providers.env"
    env_path.write_text(
        "KINAIYA_CUSTOM_PROVIDER_IDS=p01,p02\n"
        'KINAIYA_PROVIDER_P01_NAME="Local Workstation"\n'
        'KINAIYA_PROVIDER_P01_BASE_URL="http://127.0.0.1:1234/v1"\n'
        'KINAIYA_PROVIDER_P01_ADAPTER="lmstudio"\n'
        'KINAIYA_PROVIDER_P01_API_KEY=""\n'
        "KINAIYA_PROVIDER_P01_ENABLED=true\n"
        'KINAIYA_PROVIDER_P01_DISCOVERED_MODELS="qwen2.5-coder,llama-3.2-3b"\n'
        'KINAIYA_PROVIDER_P02_NAME="Cloud Meta Gateway"\n'
        'KINAIYA_PROVIDER_P02_BASE_URL="https://cloud.example.com/v1"\n'
        'KINAIYA_PROVIDER_P02_ADAPTER="meta"\n'
        'KINAIYA_PROVIDER_P02_API_KEY="sk-cloud-p02-secret"\n'
        "KINAIYA_PROVIDER_P02_ENABLED=true\n"
        'KINAIYA_PROVIDER_P02_DISCOVERED_MODELS="qwen2.5-coder,muse-spark"\n',
        encoding="utf-8",
    )

    profiles = load_stored_profiles(env_path=env_path)
    assert len(profiles) == 2
    p01, p02 = profiles[0], profiles[1]
    assert "sk-cloud-p02-secret" not in repr(p02)

    tty1 = _FakeTTY(["1"])  # Pick qwen2.5-coder on p01 for agent
    sel_p01 = select_model_interactively(
        tty1,
        p01.discovered_models,
        profile_id=p01.profile_id,
        role="agent",
        adapter_kind=p01.adapter_kind,
        endpoint=p01.base_url,
        credential_env=p01.credential_env,
        allow_cloud=p01.allow_cloud,
    )
    tty2 = _FakeTTY(["1"])  # Pick qwen2.5-coder on p02 for converse
    sel_p02 = select_model_interactively(
        tty2,
        p02.discovered_models,
        profile_id=p02.profile_id,
        role="converse",
        adapter_kind=p02.adapter_kind,
        endpoint=p02.base_url,
        credential_env=p02.credential_env,
        allow_cloud=p02.allow_cloud,
    )

    assert sel_p01.model_id == "qwen2.5-coder"
    assert sel_p02.model_id == "qwen2.5-coder"
    assert sel_p01.identity == ("p01", "qwen2.5-coder")
    assert sel_p02.identity == ("p02", "qwen2.5-coder")
    assert sel_p01.identity != sel_p02.identity

    config_path = tmp_path / "config.json"
    save_role_model_selection(config_path, sel_p01)
    save_role_model_selection(config_path, sel_p02)

    reloaded_agent = load_role_model_selection(config_path, "agent")
    reloaded_converse = load_role_model_selection(config_path, "converse")

    assert reloaded_agent.identity == ("p01", "qwen2.5-coder")
    assert reloaded_agent.qualified_id == "p01:qwen2.5-coder"
    assert reloaded_agent.endpoint == "http://127.0.0.1:1234/v1"
    assert reloaded_agent.credential_env is None

    assert reloaded_converse.identity == ("p02", "qwen2.5-coder")
    assert reloaded_converse.qualified_id == "p02:qwen2.5-coder"
    assert reloaded_converse.endpoint == "https://cloud.example.com/v1"
    assert reloaded_converse.credential_env == "KINAIYA_PROVIDER_P02_API_KEY"


@pytest.mark.parametrize(
    "case,expected_status,expected_warning",
    [
        ("empty", "empty", "0 models"),
        ("unauthorized_401", "unauthorized", "HTTP 401"),
        ("unsupported_404", "unsupported_endpoint", "HTTP 404"),
        ("redirect_302", "redirect_blocked", "Redirect (HTTP 302)"),
        ("timeout", "timeout", "Timed out"),
        ("conn_refused", "connection_error", "Could not connect"),
        ("malformed_json", "malformed_response", "malformed JSON"),
        ("missing_data_key", "malformed_response", "did not contain a 'data' or 'models' array"),
        ("oversized_body", "malformed_response", "exceeded"),
        ("unapproved_cloud", "disclosure_required", "Cloud disclosure approval is required"),
    ],
)
def test_discovery_failures_show_truthful_warning_and_fall_back_to_manual_entry(
    case: str, expected_status: str, expected_warning: str
) -> None:
    is_cloud = case == "unapproved_cloud"
    prof = ProviderProfileChoice(
        profile_id="p03",
        name="Test Endpoint",
        base_url="https://remote.example.com/v1" if is_cloud else "http://127.0.0.1:1234/v1",
        adapter_kind="meta" if is_cloud else "lmstudio",
        allow_cloud=True if is_cloud else False,
        api_key="sk-secret-never-leak-in-output",
    )

    def fake_opener(req: Any, timeout: float = 5.0) -> _FakeHTTPResponse:
        assert req.get_method() == "GET"
        assert req.full_url.endswith("/v1/models")
        if case == "empty":
            return _FakeHTTPResponse(b'{"data": []}', 200)
        if case == "unauthorized_401":
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        if case == "unsupported_404":
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
        if case == "redirect_302":
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {}, None)
        if case == "timeout":
            raise TimeoutError("timed out")
        if case == "conn_refused":
            raise urllib.error.URLError("Connection refused")
        if case == "malformed_json":
            return _FakeHTTPResponse(b"{not-valid-json", 200)
        if case == "missing_data_key":
            return _FakeHTTPResponse(b'{"status": "ok"}', 200)
        if case == "oversized_body":
            return _FakeHTTPResponse(b"x" * 70000, 200)
        raise AssertionError("unapproved_cloud must not invoke opener")

    disc = discover_profile_models(
        prof,
        allow_cloud_disclosed=not is_cloud,
        opener=fake_opener,
    )
    assert disc.status == expected_status
    assert disc.models == ()
    assert disc.manual_fallback_required is True
    assert expected_warning in disc.message
    assert "sk-secret-never-leak-in-output" not in disc.message

    tty = _FakeTTY(["manual-fallback-model-7b"])
    selected = select_model_interactively(
        tty,
        disc,
        profile_id="p03",
        role="agent",
        adapter_kind=prof.adapter_kind,
        endpoint=prof.base_url,
        allow_cloud=prof.allow_cloud,
    )
    assert selected.identity == ("p03", "manual-fallback-model-7b")
    assert selected.selection_source == "manual"
    assert "Warning:" in tty.output
    normalized_output = " ".join(tty.output.split())
    assert expected_warning in normalized_output
    assert "Falling back to manual model ID entry." in normalized_output
    assert "sk-secret-never-leak-in-output" not in tty.output


def test_unsupported_agent_adapter_compatible_is_flagged_and_refused_for_agent_but_allowed_for_converse() -> (
    None
):
    prof_compat = ProviderProfileChoice(
        profile_id="p04",
        name="Local OpenAI-Compatible Server",
        base_url="http://127.0.0.1:8080/v1",
        adapter_kind="compatible",
        discovered_models=("qwen2.5-coder",),
    )

    agent_elig = check_role_adapter_eligibility("agent", "compatible")
    assert agent_elig.eligible is False
    assert "prepare_complete" in agent_elig.reason

    converse_elig = check_role_adapter_eligibility("converse", "compatible")
    assert converse_elig.eligible is True

    decisions_elig = check_role_adapter_eligibility("decisions", "compatible")
    assert decisions_elig.eligible is True

    table_agent = format_profile_choices([prof_compat], role="agent", width=100)
    assert "UNSUPPORTED (#335)" in table_agent
    assert "prepare_complete" in table_agent

    # Selecting p04 for Agent is refused in both profile selector and model selector
    with pytest.raises(InstallError, match="prepare_complete"):
        select_profile_interactively(_FakeTTY(["1"]), [prof_compat], role="agent")

    with pytest.raises(InstallError, match="prepare_complete"):
        select_model_interactively(
            _FakeTTY([""]),
            prof_compat.discovered_models,
            profile_id="p04",
            role="agent",
            adapter_kind="compatible",
            endpoint=prof_compat.base_url,
        )

    # Selecting p04 for Converse and Decisions succeeds
    chosen_conv_prof = select_profile_interactively(_FakeTTY(["1"]), [prof_compat], role="converse")
    assert chosen_conv_prof.profile_id == "p04"
    chosen_conv_model = select_model_interactively(
        _FakeTTY([""]),
        prof_compat.discovered_models,
        profile_id="p04",
        role="converse",
        adapter_kind="compatible",
        endpoint=prof_compat.base_url,
    )
    assert chosen_conv_model.identity == ("p04", "qwen2.5-coder")


def test_narrow_terminal_width_40_cols_pagination_large_cap_cancel_and_no_tty() -> None:
    # 1. No-TTY refusal
    with pytest.raises(InstallError, match="No controlling terminal"):
        select_model_interactively(None, ["model-a"], profile_id="p01")

    # 2. Large model list (>500) is capped at MAX_DISCOVERED_MODELS (500)
    huge_payload = json.dumps(
        {"data": [{"id": f"synthetic-model-{i:04d}"} for i in range(620)]}
    ).encode("utf-8")
    prof = ProviderProfileChoice(
        profile_id="p01",
        name="Local Bulk Server",
        base_url="http://127.0.0.1:1234/v1",
        adapter_kind="lmstudio",
    )
    disc = discover_profile_models(
        prof,
        opener=lambda req, timeout=5.0: _FakeHTTPResponse(huge_payload, 200),
    )
    assert disc.status == "ok"
    assert disc.truncated is True
    assert len(disc.models) == MAX_DISCOVERED_MODELS

    # 3. Narrow terminal (40 cols) + next page ('n') + search + manual override
    tty_narrow = _FakeTTY(["n", "/0499", ""])
    selected = select_model_interactively(
        tty_narrow,
        disc,
        profile_id="p01",
        role="agent",
        adapter_kind="lmstudio",
        width=40,
        page_size=10,
    )
    assert selected.identity == ("p01", "synthetic-model-0499")
    for line in tty_narrow.output.splitlines():
        assert len(line) <= 40

    # 4. Explicit cancellation (:cancel) raises InstallError
    tty_cancel = _FakeTTY([":cancel"])
    with pytest.raises(InstallError, match="Setup cancelled"):
        select_model_interactively(
            tty_cancel,
            ["model-1", "model-2"],
            profile_id="p01",
            role="agent",
            adapter_kind="lmstudio",
        )


def test_wizard_integration_with_stored_profiles_and_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import launcher

    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    layout = Layout.for_home(tmp_path)
    profiles = [
        ProviderProfileChoice(
            profile_id="p01",
            name="Local LM Studio",
            base_url="http://127.0.0.1:1234",
            adapter_kind="lmstudio",
            allow_cloud=False,
        )
    ]
    payload = b'{"data": [{"id": "qwen2.5-coder-7b"}, {"id": "deepseek-coder-v2"}]}'
    tty = _FakeTTY(
        [
            "",  # workspace
            "",  # data
            "1",  # select profile 1 (p01)
            "/deepseek",  # filter models
            "",  # Enter selects sole filtered match deepseek-coder-v2
            "no",  # converse
            "yes",  # confirm write
        ]
    )
    res = wizard.run_wizard(
        layout,
        tty=tty,
        profiles=profiles,
        discover_models=True,
        opener=lambda req, timeout=5.0: _FakeHTTPResponse(payload, 200),
    )
    assert res["ready"] is False
    cfg = json.loads(layout.config.read_text(encoding="utf-8"))
    assert cfg["providers"]["agent"]["model"] == "deepseek-coder-v2"
    assert cfg["providers"]["agent"]["kind"] == "lmstudio"

    # Also verify normal wizard flow (profiles=None) loads persisted providers.env profiles
    layout2 = Layout.for_home(tmp_path / "persisted-home")
    layout2.data.mkdir(parents=True, exist_ok=True, mode=0o700)
    (layout2.data / "providers.env").write_text(
        "KINAIYA_CUSTOM_PROVIDER_IDS=p01\n"
        'KINAIYA_PROVIDER_P01_NAME="Persisted Local"\n'
        'KINAIYA_PROVIDER_P01_BASE_URL="http://127.0.0.1:1234"\n'
        'KINAIYA_PROVIDER_P01_ADAPTER="lmstudio"\n'
        'KINAIYA_PROVIDER_P01_API_KEY=""\n'
        "KINAIYA_PROVIDER_P01_ENABLED=true\n"
        'KINAIYA_PROVIDER_P01_DISCOVERED_MODELS="qwen2.5-coder-7b,deepseek-coder-v2"\n',
        encoding="utf-8",
    )
    tty2 = _FakeTTY(
        [
            "",  # workspace
            "",  # data
            "p01",  # pick persisted profile p01
            "2",  # select model 2 (deepseek-coder-v2)
            "no",  # converse
            "yes",  # confirm write
        ]
    )
    res2 = wizard.run_wizard(layout2, tty=tty2)
    assert res2["ready"] is False
    reloaded = load_role_model_selection(layout2.config, "agent")
    assert reloaded.identity == ("p01", "deepseek-coder-v2")
    assert reloaded.qualified_id == "p01:deepseek-coder-v2"


def test_starter_provider_presets_and_roster():
    """Issue #503: starter presets provide LM Studio and Meta cloud with proper adapters."""
    presets = get_starter_provider_presets()
    assert len(presets) >= 2
    lmstudio_preset = presets[0]
    assert lmstudio_preset.profile_id == "p01"
    assert lmstudio_preset.adapter_kind == "lmstudio"
    assert not lmstudio_preset.allow_cloud
    assert "127.0.0.1:1234" in lmstudio_preset.base_url

    meta_preset = presets[1]
    assert meta_preset.profile_id == "p02"
    assert meta_preset.adapter_kind == "meta"
    assert meta_preset.allow_cloud
    assert meta_preset.credential_env == "KINAIYA_META_API_KEY"

    # load_stored_profiles with include_starter_presets=True on clean machine
    loaded = load_stored_profiles(include_starter_presets=True)
    assert len(loaded) >= 2
    assert loaded[0].profile_id == "p01"


def test_check_local_v1_hint():
    """Issue #503: smart /v1 local server detection."""
    # Ollama on 11434 lacking /v1
    assert check_local_v1_hint("http://localhost:11434") == "http://localhost:11434/v1"
    assert check_local_v1_hint("http://127.0.0.1:11434") == "http://127.0.0.1:11434/v1"
    # llama.cpp on 8080 lacking /v1
    assert check_local_v1_hint("http://127.0.0.1:8080") == "http://127.0.0.1:8080/v1"

    # Already has /v1 -> no hint
    assert check_local_v1_hint("http://localhost:11434/v1") is None
    assert check_local_v1_hint("http://127.0.0.1:1234/v1") is None

    # Remote cloud url without standard local ports -> no hint
    assert check_local_v1_hint("https://api.openai.com/v1") is None
    assert check_local_v1_hint("https://api.example.com") is None


def test_inline_custom_endpoint_with_v1_correction_in_wizard(tmp_path: Path):
    """Issue #503: wizard inline custom endpoint with /v1 correction prompt."""
    layout = Layout.for_home(tmp_path)
    tty = _FakeTTY(
        [
            "",  # workspace
            "",  # data
            "3",  # provider option 3: Custom OpenAI-compatible
            "http://localhost:11434",  # entered without /v1
            "y",  # accept /v1 hint -> becomes http://localhost:11434/v1
            "my-custom-ollama-model",  # model
            "no",  # converse
            "yes",  # confirm write
        ]
    )
    res = wizard.run_wizard(layout, tty=tty)
    assert res["ready"] is False
    cfg = json.loads(layout.config.read_text(encoding="utf-8"))
    assert cfg["providers"]["agent"]["base_url"] == "http://localhost:11434/v1"
    assert cfg["providers"]["agent"]["model"] == "my-custom-ollama-model"
    assert cfg["providers"]["agent"]["kind"] == "lmstudio"
    assert "Hint: Local servers usually require /v1 suffix" in tty.output


def test_remote_cloud_disclosure_and_key_before_model_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #502: cloud disclosure & API key must precede GET /models discovery."""
    from services import launcher

    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setenv("MY_TEST_CLOUD_KEY", "sk-cloud-secret-12345")
    layout = Layout.for_home(tmp_path)
    cloud_profile = ProviderProfileChoice(
        profile_id="p02",
        name="Meta Cloud",
        base_url="https://api.together.xyz/v1",
        adapter_kind="meta",
        allow_cloud=True,
        credential_env="MY_TEST_CLOUD_KEY",
        models_path="/models",
    )
    payload = b'{"data": [{"id": "meta-llama/Llama-3.3-70B-Instruct"}]}'
    captured_headers: dict[str, str] = {}

    def mock_opener(req, timeout=5.0):
        for k, v in req.headers.items():
            captured_headers[k.lower()] = v
        return _FakeHTTPResponse(payload, 200)

    tty = _FakeTTY(
        [
            "",  # workspace
            "",  # data
            "1",  # select profile 1 (p02 cloud)
            "yes",  # cloud disclosure prompt BEFORE discovery (#502)
            ":env MY_TEST_CLOUD_KEY",  # API key prompt BEFORE discovery (#502)
            "meta-llama/Llama-3.3-70B-Instruct",  # select discovered model
            "3",  # topology: Agent only
            "yes",  # confirm write
        ]
    )
    res = wizard.run_wizard(
        layout,
        tty=tty,
        profiles=[cloud_profile],
        discover_models=True,
        opener=mock_opener,
    )
    assert res["ready"] is False
    assert captured_headers.get("authorization") == "Bearer sk-cloud-secret-12345"
    cfg = json.loads(layout.config.read_text(encoding="utf-8"))
    assert cfg["providers"]["agent"]["model"] == "meta-llama/Llama-3.3-70B-Instruct"
    assert cfg["providers"]["agent"]["allow_cloud"] is True


def test_remote_cloud_denied_disclosure_avoids_network_and_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #502: denying cloud disclosure makes 0 network calls and falls back cleanly."""
    from services import launcher

    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    layout = Layout.for_home(tmp_path)
    cloud_profile = ProviderProfileChoice(
        profile_id="p02",
        name="Meta Cloud",
        base_url="https://api.together.xyz/v1",
        adapter_kind="meta",
        allow_cloud=True,
        credential_env="KINAIYA_META_API_KEY",
        models_path="/models",
    )
    network_called = False

    def refusing_opener(req, timeout=5.0):
        nonlocal network_called
        network_called = True
        raise AssertionError("Network call must not occur when cloud disclosure is denied")

    tty = _FakeTTY(
        [
            "",  # workspace
            "",  # data
            "1",  # select profile 1 (p02 cloud)
            "no",  # cloud disclosure prompt: DENIED (#502)
            "manual-cloud-model",  # manual fallback model ID
            "no",  # converse
            "yes",  # confirm write
        ]
    )
    with pytest.raises(
        InstallError,
        match="Use a credential-free loopback endpoint, or HTTPS with explicit cloud disclosure",
    ):
        wizard.run_wizard(
            layout,
            tty=tty,
            profiles=[cloud_profile],
            discover_models=True,
            opener=refusing_opener,
        )
    assert network_called is False


def test_pre_setup_config_backup_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #502: pre-setup config backup is saved before rewriting config.json."""
    from services import launcher

    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    layout = Layout.for_home(tmp_path)
    layout.config.parent.mkdir(parents=True, exist_ok=True)
    initial_content = '{"version": 1, "custom_field": "do-not-lose"}'
    layout.config.write_text(initial_content, encoding="utf-8")

    tty = _FakeTTY(
        [
            "",  # workspace
            "",  # data
            "http://127.0.0.1:1234",  # endpoint
            "new-agent-model",  # model
            "no",  # allow_cloud
            "",  # cred env
            "3",  # topology: option 3 (Agent only)
            "yes",  # confirm write
        ]
    )
    res = wizard.run_wizard(layout, tty=tty)
    assert res["ready"] is False
    bak_file = layout.config.with_name(f"{layout.config.name}.pre-setup.bak")
    assert bak_file.exists()
    assert bak_file.read_text(encoding="utf-8") == initial_content
