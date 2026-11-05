"""Terminal first-run custom API profile creation (#402), synthetic fixtures only."""

from __future__ import annotations

import stat
from types import SimpleNamespace

import pytest

from services.distribution import profile_form, wizard
from services.distribution.profile_form import (
    ProfileFormCancelled,
    choose_or_add_profile,
    run_add_custom_api,
)
from services.settings.custom_providers_env import CustomProviderError, CustomProvidersEnvStore

SECRET = "sk-synthetic-never-print-0001"
SECRET_2 = "sk-synthetic-never-print-0002"


class FakeTTY:
    def __init__(self, answers):
        self.answers = list(answers)
        self.output = ""

    def write(self, value):
        self.output += value

    def flush(self):
        pass

    def readline(self):
        return (self.answers.pop(0) + "\n") if self.answers else ""


def secrets(*values):
    queue = list(values)

    def read_secret(tty, label):
        tty.write(label)
        return queue.pop(0)

    return read_secret


@pytest.fixture
def paths(tmp_path):
    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    data.mkdir(mode=0o700)
    workspace.mkdir()
    return SimpleNamespace(data=data, workspace=workspace, env=data / "custom_providers.env")


def store_for(paths):
    return CustomProvidersEnvStore(paths.env, workspace=paths.workspace)


def test_zero_profiles_creates_one_profile_and_returns_stable_id(paths):
    store = store_for(paths)
    tty = FakeTTY(["Local LM", "http://127.0.0.1:1234", "y"])
    profile_id = choose_or_add_profile(store, tty, read_secret=secrets(""))
    assert profile_id == "p01"
    profile = store.snapshot()["profiles"][0]
    assert profile["name"] == "Local LM"
    assert profile["base_url"] == "http://127.0.0.1:1234/v1"
    assert profile["has_api_key"] is False
    assert "does not contact the provider" in tty.output
    assert stat.S_IMODE(paths.env.stat().st_mode) == 0o600


def test_masked_key_is_saved_but_never_printed(paths):
    store = store_for(paths)
    tty = FakeTTY(["Gateway", "https://gateway.example/v1", "y"])
    created = run_add_custom_api(store, tty, read_secret=secrets(SECRET))
    assert created.profile_id == "p01"
    assert SECRET not in tty.output
    assert "set (hidden)" in tty.output
    assert store.snapshot()["profiles"][0]["has_api_key"] is True
    assert SECRET not in repr(created)


def test_second_profile_with_duplicate_name_preserves_first_and_unrelated_lines(paths):
    paths.env.write_text("UNRELATED_SETTING=keep-me\n", encoding="utf-8")
    paths.env.chmod(0o600)
    store = store_for(paths)
    run_add_custom_api(
        store, FakeTTY(["Gateway", "https://a.example/v1", "y"]), read_secret=secrets(SECRET)
    )
    tty = FakeTTY(["a", "Gateway", "https://b.example/v1", "y"])
    second = choose_or_add_profile(store, tty, read_secret=secrets(SECRET_2))
    assert second == "p02"
    assert "Gateway [p01]" in tty.output
    profiles = {p["profile_id"]: p for p in store.snapshot()["profiles"]}
    assert profiles["p01"]["base_url"] == "https://a.example/v1"
    assert profiles["p02"]["base_url"] == "https://b.example/v1"
    text = paths.env.read_text(encoding="utf-8")
    assert "UNRELATED_SETTING=keep-me" in text
    assert SECRET in text and SECRET_2 in text


def test_existing_profile_can_be_chosen_without_writing(paths):
    store = store_for(paths)
    run_add_custom_api(
        store, FakeTTY(["Only", "https://only.example/v1", "y"]), read_secret=secrets("")
    )
    before = paths.env.read_bytes()
    assert choose_or_add_profile(store, FakeTTY(["9", "1"]), read_secret=secrets()) == "p01"
    assert paths.env.read_bytes() == before


def test_invalid_url_is_reported_and_non_secret_answers_are_retained(paths):
    store = store_for(paths)
    tty = FakeTTY(["Named", "ftp://bad.example", "http://remote.example/v1", "", "y"])
    with pytest.raises(ProfileFormCancelled):
        # Remote plain HTTP is refused by the shared validator; the empty answer keeps
        # the previous (invalid) URL, so the form re-prompts until input closes.
        run_add_custom_api(store, tty, read_secret=secrets(""))
    assert "scheme" in tty.output.lower()
    assert "Remote HTTP" in tty.output
    assert store.snapshot()["profiles"] == []
    ok = FakeTTY(["Named", "ftp://bad.example", "https://ok.example/v1", "y"])
    assert run_add_custom_api(store, ok, read_secret=secrets("")).profile_id == "p01"


def test_back_navigation_keeps_answers_and_key(paths):
    store = store_for(paths)
    tty = FakeTTY(["First", "<", "", "https://x.example/v1", "<", "y"])
    created = run_add_custom_api(store, tty, read_secret=secrets(SECRET, ""))
    assert created.name == "First"
    assert store.snapshot()["profiles"][0]["has_api_key"] is True
    assert "Enter keeps the key you typed" in tty.output
    assert SECRET not in tty.output


@pytest.mark.parametrize(
    "answers",
    [
        ["Name", "https://x.example/v1", "n"],
        ["Name", ":cancel"],
        ["<"],
        [],
    ],
    ids=["decline-confirm", "cancel-token", "back-from-first", "no-tty-eof"],
)
def test_cancel_back_and_no_tty_write_nothing(paths, answers):
    store = store_for(paths)
    with pytest.raises(ProfileFormCancelled):
        run_add_custom_api(store, FakeTTY(answers), read_secret=secrets(SECRET))
    assert not paths.env.exists()


def test_save_failure_leaves_no_partial_profile_and_retry_succeeds(paths, monkeypatch):
    store = store_for(paths)
    real = store.add_profile
    failures = [CustomProviderError("Provider name must not be empty."), OSError("disk full")]

    def flaky(**kwargs):
        if failures:
            raise failures.pop(0)
        return real(**kwargs)

    monkeypatch.setattr(store, "add_profile", flaky)
    tty = FakeTTY(["Retry", "https://r.example/v1", "y", "", "", "y", "", "", "y"])
    created = run_add_custom_api(store, tty, read_secret=secrets(SECRET, "", ""))
    assert created.profile_id == "p01"
    assert tty.output.count("Could not save the profile") == 2
    assert "storage error" in tty.output and "disk full" not in tty.output
    assert SECRET not in tty.output
    assert len(store.snapshot()["profiles"]) == 1
    assert store.snapshot()["profiles"][0]["has_api_key"] is True


def test_wizard_offers_profile_after_config_and_returns_id(tmp_path, paths, monkeypatch):
    monkeypatch.setattr(wizard, "check_outdated_system_blockers", lambda **kw: None)
    monkeypatch.setattr(wizard, "setup", lambda chosen, **kw: {"ready": False})
    layout = SimpleNamespace(
        workspace=paths.workspace, data=paths.data, config=tmp_path / "config.json"
    )
    monkeypatch.setattr(wizard, "replace", lambda base, **kw: layout)
    base = ["", "", "", "local-model", "", "", "", "yes"]
    tty = FakeTTY([*base, "y", "Local LM", "http://127.0.0.1:1234/v1", "y"])
    result = wizard.run_wizard(
        layout,
        tty=tty,
        store_factory=lambda chosen: store_for(paths),
        read_secret=secrets(""),
    )
    assert result["ready"] is False
    assert result["custom_profile_id"] == "p01"

    declined = wizard.run_wizard(
        layout, tty=FakeTTY([*base, ""]), store_factory=lambda chosen: store_for(paths)
    )
    assert declined["ready"] is False
    assert "custom_profile_id" not in declined

    cancelled_tty = FakeTTY([*base, "y", "1"])
    picked = wizard.run_wizard(
        layout, tty=cancelled_tty, store_factory=lambda chosen: store_for(paths)
    )
    assert picked["custom_profile_id"] == "p01"


def test_default_store_uses_the_host_custom_providers_file(paths):
    store = profile_form.custom_providers_store(paths)
    assert store.env_path == paths.env
