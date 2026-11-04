"""Issue #389: Setup wizard masked API-key entry saved to private local credentials."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from services.distribution import wizard
from services.distribution.credentials import (
    MASKED_KEY_INDICATOR,
    LocalCredentialsStore,
    default_providers_env_path,
    profile_credential_env,
    prompt_masked_api_key,
)
from services.distribution.installation import InstallError, Layout


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


def test_masked_api_key_lifecycle_skip_set_keep_replace_and_remove(tmp_path: Path) -> None:
    env_path = tmp_path / "data" / "providers.env"
    store = LocalCredentialsStore(env_path)

    # 1. Optional empty key on fresh profile -> skip, no file written yet.
    tty_skip = _FakeTTY([""])
    act_skip = prompt_masked_api_key(
        tty_skip,
        profile_id="p01",
        endpoint="http://127.0.0.1:1234",
        env_path=env_path,
        commit=True,
    )
    assert act_skip.action == "skip"
    assert act_skip.credential_env is None
    assert act_skip.credential_ref is None
    assert not env_path.exists()

    # 2. New key entry -> set, persisted to 0600 providers.env, never echoed to TTY.
    secret_v1 = "sk-proj-synthetic-secret-v1-998877"
    tty_set = _FakeTTY([secret_v1])
    act_set = prompt_masked_api_key(
        tty_set,
        profile_id="p01",
        endpoint="https://api.example.com/v1",
        env_path=env_path,
        profile_name="Primary Cloud",
        commit=True,
    )
    assert act_set.action == "set"
    assert act_set.credential_env == "KINAIYA_PROVIDER_P01_API_KEY"
    assert act_set.credential_ref == "KINAIYA_PROVIDER_P01_API_KEY"
    assert secret_v1 not in tty_set.output
    assert secret_v1 not in repr(act_set)
    assert secret_v1 not in str(act_set)
    assert MASKED_KEY_INDICATOR in tty_set.output
    assert "0600" in tty_set.output and "plaintext" in tty_set.output

    # Reload from disk across simulated restart.
    reloaded = LocalCredentialsStore(env_path)
    assert reloaded.has_api_key("p01") is True
    assert reloaded.get_api_key("p01") == secret_v1
    prof = reloaded.get_profile("p01")
    assert prof is not None
    assert prof.name == "Primary Cloud"
    assert prof.base_url == "https://api.example.com/v1"
    assert prof.masked_api_key == MASKED_KEY_INDICATOR
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    # 3. Keep existing key on blank Enter -> keep, key remains secret_v1.
    tty_keep = _FakeTTY([""])
    act_keep = prompt_masked_api_key(
        tty_keep,
        profile_id="p01",
        endpoint="https://api.example.com/v1",
        env_path=env_path,
        commit=True,
    )
    assert act_keep.action == "keep"
    assert act_keep.credential_env == "KINAIYA_PROVIDER_P01_API_KEY"
    assert LocalCredentialsStore(env_path).get_api_key("p01") == secret_v1
    assert secret_v1 not in tty_keep.output

    # 4. Replace key -> set, updates only p01 key to secret_v2.
    secret_v2 = "sk-proj-synthetic-secret-v2-112233"
    tty_replace = _FakeTTY([secret_v2])
    act_replace = prompt_masked_api_key(
        tty_replace,
        profile_id="p01",
        endpoint="https://api.example.com/v1",
        env_path=env_path,
        commit=True,
    )
    assert act_replace.action == "set"
    assert LocalCredentialsStore(env_path).get_api_key("p01") == secret_v2
    assert secret_v1 not in env_path.read_text(encoding="utf-8")
    assert secret_v2 not in tty_replace.output

    # 5. Explicit removal via :remove -> clears stored key.
    tty_remove = _FakeTTY([":remove"])
    act_remove = prompt_masked_api_key(
        tty_remove,
        profile_id="p01",
        endpoint="https://api.example.com/v1",
        env_path=env_path,
        commit=True,
    )
    assert act_remove.action == "remove"
    assert act_remove.credential_env is None
    assert store.has_api_key("p01") is False
    assert store.get_api_key("p01") == ""
    assert secret_v2 not in env_path.read_text(encoding="utf-8")


def test_six_stored_profiles_and_unrelated_dotenv_variables_do_not_clobber(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / "providers.env"
    env_path.write_text(
        "# User custom comments and unrelated variables must stay intact\n"
        'UNRELATED_BEFORE="keep-before-value"\n'
        "EXPORTED_SAMPLE=42\n",
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)

    store = LocalCredentialsStore(env_path)
    expected_keys: dict[str, str] = {}
    for idx in range(1, 7):
        pid = f"p{idx:02d}"
        secret = f"sk-profile-{pid}-unique-secret-{idx * 1111}"
        expected_keys[pid] = secret
        store.set_profile_key(
            pid,
            secret,
            name=f"Provider {idx}",
            base_url=f"https://provider-{idx}.example.com/v1",
            selected_model=f"model-{idx}",
        )

    # Append another unrelated variable and then update p03 and remove p05 key.
    with env_path.open("a", encoding="utf-8") as handle:
        handle.write('UNRELATED_AFTER="keep-after-value"\n')

    expected_keys["p03"] = "sk-profile-p03-rotated-99999"
    store.set_profile_key("p03", expected_keys["p03"])
    store.remove_profile_key("p05")
    expected_keys["p05"] = ""

    reloaded = LocalCredentialsStore(env_path)
    env_map = reloaded.load_env_map()
    assert env_map["UNRELATED_BEFORE"] == "keep-before-value"
    assert env_map["EXPORTED_SAMPLE"] == "42"
    assert env_map["UNRELATED_AFTER"] == "keep-after-value"
    assert env_map["KINAIYA_CUSTOM_PROVIDER_IDS"] == "p01,p02,p03,p04,p05,p06"

    for idx in range(1, 7):
        pid = f"p{idx:02d}"
        assert reloaded.get_api_key(pid) == expected_keys[pid]
        prof = reloaded.get_profile(pid)
        assert prof is not None
        assert prof.name == f"Provider {idx}"
        assert prof.base_url == f"https://provider-{idx}.example.com/v1"
        assert prof.selected_model == f"model-{idx}"
        assert prof.has_api_key is (pid != "p05")

    raw_text = env_path.read_text(encoding="utf-8")
    assert "# User custom comments and unrelated variables must stay intact" in raw_text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_wizard_interactive_key_persists_across_restart_and_cancellation_leaves_env_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import launcher

    monkeypatch.setattr(
        launcher,
        "load_or_create_tokens",
        lambda directory: directory.mkdir(parents=True, exist_ok=True),
    )
    layout = Layout.for_home(tmp_path)
    env_path = default_providers_env_path(layout)

    # 1. Cancel before final confirmation -> neither config.json nor providers.env is written.
    secret_cancelled = "sk-cancelled-secret-should-never-persist"
    tty_cancel = _FakeTTY(
        [
            "",
            "",
            "meta=https://api.meta.ai/v1",
            "muse-spark-1.3-contributor",
            "yes",
            secret_cancelled,
            "no",
            "no",  # Cancel at final confirmation
        ]
    )
    with pytest.raises(InstallError, match="Setup cancelled; no configuration written"):
        wizard.run_wizard(layout, tty=tty_cancel, profile_id="p01")
    assert not layout.config.exists()
    assert not env_path.exists()
    assert secret_cancelled not in tty_cancel.output

    # 2. Confirm wizard run with masked API key -> saves reference in config.json and key in providers.env.
    secret_saved = "sk-saved-cloud-secret-424242"
    tty_save = _FakeTTY(
        [
            "",
            "",
            "meta=https://api.meta.ai/v1",
            "muse-spark-1.3-contributor",
            "yes",
            secret_saved,
            "no",
            "yes",
        ]
    )
    result = wizard.run_wizard(layout, tty=tty_save, profile_id="p01")
    assert result["ready"] is False
    assert secret_saved not in tty_save.output

    config_text = layout.config.read_text(encoding="utf-8")
    assert secret_saved not in config_text
    config = json.loads(config_text)
    assert config["providers"]["agent"]["api_key_env"] == profile_credential_env("p01")

    store = LocalCredentialsStore(env_path)
    assert store.get_api_key("p01") == secret_saved
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600

    # 3. Re-run wizard pressing Enter on key prompt -> keeps existing key in providers.env.
    tty_keep = _FakeTTY(
        [
            "",
            "",
            "meta=https://api.meta.ai/v1",
            "muse-spark-1.3-contributor",
            "yes",
            "",  # Blank Enter keeps existing key
            "no",
            "yes",
        ]
    )
    wizard.run_wizard(layout, tty=tty_keep, profile_id="p01")
    assert LocalCredentialsStore(env_path).get_api_key("p01") == secret_saved
    assert "[kept existing" in tty_keep.output

    # 4. Re-run wizard and cancel when replacing key -> previous key in providers.env is untouched!
    tty_cancel_replace = _FakeTTY(
        [
            "",
            "",
            "meta=https://api.meta.ai/v1",
            "muse-spark-1.3-contributor",
            "yes",
            "sk-uncommitted-replacement-999",
            "no",
            "no",
        ]
    )
    with pytest.raises(InstallError, match="Setup cancelled"):
        wizard.run_wizard(layout, tty=tty_cancel_replace, profile_id="p01")
    assert LocalCredentialsStore(env_path).get_api_key("p01") == secret_saved


def test_env_ref_modes_and_getpass_callback(tmp_path: Path) -> None:
    env_path = tmp_path / "providers.env"

    # :env VAR_NAME
    tty1 = _FakeTTY([":env CUSTOM_EXTERNAL_API_KEY"])
    act1 = prompt_masked_api_key(tty1, profile_id="p02", env_path=env_path, commit=True)
    assert act1.action == "env_ref"
    assert act1.credential_env == "CUSTOM_EXTERNAL_API_KEY"
    assert not env_path.exists()

    # ENV:VAR_NAME
    tty2 = _FakeTTY(["ENV:SECOND_EXTERNAL_KEY"])
    act2 = prompt_masked_api_key(tty2, profile_id="p02", env_path=env_path, commit=True)
    assert act2.action == "env_ref"
    assert act2.credential_env == "SECOND_EXTERNAL_KEY"

    # Bare :env followed by variable name prompt
    tty3 = _FakeTTY([":env", "THIRD_EXTERNAL_KEY"])
    act3 = prompt_masked_api_key(tty3, profile_id="p02", env_path=env_path, commit=True)
    assert act3.action == "env_ref"
    assert act3.credential_env == "THIRD_EXTERNAL_KEY"

    # Invalid env var name via :env is rejected
    tty_bad = _FakeTTY([":env not-an-env-var!"])
    with pytest.raises(InstallError, match="environment variable NAME only"):
        prompt_masked_api_key(tty_bad, profile_id="p02")

    # getpass_fn callback support without echoing
    tty_gp = _FakeTTY([])
    secret_gp = "sk-via-getpass-callback-777"
    act_gp = prompt_masked_api_key(
        tty_gp,
        profile_id="p02",
        env_path=env_path,
        getpass_fn=lambda _prompt: secret_gp,
        commit=True,
    )
    assert act_gp.action == "set"
    assert secret_gp not in tty_gp.output
    assert LocalCredentialsStore(env_path).get_api_key("p02") == secret_gp


def test_symlink_nofollow_control_chars_and_shell_expressions_Remain_literal(
    tmp_path: Path,
) -> None:
    real_env = tmp_path / "real.env"
    real_env.write_text("KEEP=1\n", encoding="utf-8")
    symlink_env = tmp_path / "symlink.env"
    symlink_env.symlink_to(real_env)

    with pytest.raises(InstallError, match="symlink"):
        LocalCredentialsStore(symlink_env)

    env_path = tmp_path / "safe" / "providers.env"
    store = LocalCredentialsStore(env_path)

    # Control characters and newlines are refused without echoing the secret.
    bad_secret = "sk-bad-secret\nINJECTED_VAR=pwned"
    with pytest.raises(InstallError, match="control characters") as exc_info:
        store.set_profile_key("p01", bad_secret)
    assert "sk-bad-secret" not in str(exc_info.value)
    assert "INJECTED_VAR" not in str(exc_info.value)

    # Shell metacharacters like $(...) and `...` and quotes are stored strictly as literal strings, never evaluated.
    tricky_secret = 'sk-literal-$(echo_not_run)-`id`-"quoted"\\backslash'
    store.set_profile_key("p01", tricky_secret, name='Name "Quoted"')
    reloaded = LocalCredentialsStore(env_path)
    assert reloaded.get_api_key("p01") == tricky_secret
    prof = reloaded.get_profile("p01")
    assert prof is not None and prof.name == 'Name "Quoted"'


def test_concurrent_writers_serialize_without_losing_profiles(tmp_path: Path) -> None:
    env_path = tmp_path / "concurrent" / "providers.env"
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker(idx: int) -> None:
        pid = f"p{idx:02d}"
        secret = f"sk-concurrent-{pid}-secret-{idx * 101}"
        try:
            store = LocalCredentialsStore(env_path)
            barrier.wait(timeout=5.0)
            store.set_profile_key(
                pid,
                secret,
                name=f"Concurrent {idx}",
                base_url=f"https://c{idx}.example.com/v1",
            )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors
    final_store = LocalCredentialsStore(env_path)
    for i in range(1, 7):
        pid = f"p{i:02d}"
        assert final_store.get_api_key(pid) == f"sk-concurrent-{pid}-secret-{i * 101}"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
