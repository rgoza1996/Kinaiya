import json
import os

import pytest

from packages.providers.openrouter_budget import CharacterBudget
from packages.providers.speech import SpeechProviderError


def test_uncertain_reservations_survive_reopen_and_are_not_refunded(tmp_path):
    path = tmp_path / "ledger.json"
    budget = CharacterBudget(path, limit_usd="0.0000062")
    first = budget.admit("hello")
    reopened = CharacterBudget(path, limit_usd="0.0000062")
    second = reopened.admit("world")
    reopened.finish(second, "gen-tts-synthetic")
    with pytest.raises(SpeechProviderError):
        budget.admit("!")
    calls = json.loads(path.read_text())["calls"]
    assert [x["state"] for x in calls] == ["reserved", "completed"]
    assert calls[0]["id"] == first
    assert all("text" not in x and "input" not in x for x in calls)


def test_unicode_uses_conservative_utf8_upper_bound(tmp_path):
    path = tmp_path / "ledger.json"
    budget = CharacterBudget(path, limit_usd="0.00000186")
    budget.admit("日")
    assert json.loads(path.read_text())["calls"][0]["utf8_bytes"] == 3
    with pytest.raises(SpeechProviderError):
        budget.admit("a")


@pytest.mark.parametrize("limit", [True, False, 0, -1, "NaN", "Infinity", 26, None])
def test_invalid_limits_refuse(tmp_path, limit):
    with pytest.raises(SpeechProviderError):
        CharacterBudget(tmp_path / "ledger", limit_usd=limit)


@pytest.mark.parametrize("text", ["", "a" * 4097, "日" * 1366, None, b"hello"])
def test_invalid_inputs_do_not_create_ledger(tmp_path, text):
    path = tmp_path / "ledger"
    with pytest.raises(SpeechProviderError):
        CharacterBudget(path).admit(text)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["ledger", "lock", "parent"])
def test_symlinks_do_not_redirect_budget(tmp_path, kind):
    safe, target = tmp_path / "safe", tmp_path / "target"
    safe.mkdir(mode=0o700)
    target.mkdir(mode=0o700)
    path = safe / "ledger"
    if kind == "parent":
        link = tmp_path / "parent-link"
        link.symlink_to(target, target_is_directory=True)
        path = link / "ledger"
    else:
        leaf = path if kind == "ledger" else safe / "ledger.lock"
        leaf.symlink_to(target / "untouched")
    with pytest.raises(SpeechProviderError):
        CharacterBudget(path).admit("hello")
    assert not (target / "untouched").exists()
    assert not (target / "ledger").exists()


def test_insecure_or_hardlinked_ledger_refuses(tmp_path):
    path = tmp_path / "ledger"
    budget = CharacterBudget(path)
    budget.admit("hello")
    os.chmod(path, 0o644)
    with pytest.raises(SpeechProviderError):
        budget.admit("world")
    os.chmod(path, 0o600)
    os.link(path, tmp_path / "hardlink")
    with pytest.raises(SpeechProviderError):
        budget.admit("world")


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "byte_bool", "cap", "rate", "id"])
def test_corrupt_ledger_fails_closed_without_replacement(tmp_path, mutation):
    path = tmp_path / "ledger"
    budget = CharacterBudget(path)
    budget.admit("hello")
    value = json.loads(path.read_text())
    if mutation == "duplicate":
        value["calls"].append(dict(value["calls"][0]))
    elif mutation == "missing":
        value.pop("calls")
    elif mutation == "byte_bool":
        value["calls"][0]["utf8_bytes"] = True
    elif mutation == "cap":
        value["limit_usd"] = "2.0"
    elif mutation == "rate":
        value["rate_usd_per_million"] = "0"
    else:
        value["calls"][0]["id"] = "invalid"
    raw = json.dumps(value)
    path.write_text(raw)
    with pytest.raises(SpeechProviderError):
        budget.admit("world")
    assert path.read_text() == raw


def test_duplicate_json_keys_and_conflicting_receipts_refuse(tmp_path):
    path = tmp_path / "ledger"
    budget = CharacterBudget(path)
    identity = budget.admit("hello")
    budget.finish(identity, "gen-synthetic")
    budget.finish(identity, "gen-synthetic")
    with pytest.raises(SpeechProviderError):
        budget.finish(identity, "gen-other")
    raw = path.read_text().replace('"version":1', '"version":1,"version":1')
    path.write_text(raw)
    with pytest.raises(SpeechProviderError):
        budget.admit("world")


def test_lock_contention_refuses_without_blocking_or_losing_costs(tmp_path):
    import fcntl

    path = tmp_path / "ledger"
    budget = CharacterBudget(path)
    budget.admit("hello")
    with open(str(path) + ".lock", "r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SpeechProviderError):
            budget.admit("world")
    assert len(json.loads(path.read_text())["calls"]) == 1


def test_failed_atomic_replace_keeps_previous_reservation(tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    budget = CharacterBudget(path)
    budget.admit("hello")
    original = path.read_bytes()

    def failed(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(os, "replace", failed)
    with pytest.raises(SpeechProviderError):
        budget.admit("world")
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".speech-budget-*"))


def test_symlinked_ancestor_cannot_split_reservations(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for path in (a, b):
        (path / "private").mkdir(parents=True, mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(a, target_is_directory=True)
    budget = CharacterBudget(alias / "private" / "ledger", limit_usd="0.0000031")
    with pytest.raises(SpeechProviderError):
        budget.admit("hello")
    alias.unlink()
    alias.symlink_to(b, target_is_directory=True)
    with pytest.raises(SpeechProviderError):
        budget.admit("hello")
    assert not (a / "private" / "ledger").exists()
    assert not (b / "private" / "ledger").exists()


def test_replaced_directory_cannot_reset_an_existing_budget(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    budget = CharacterBudget(parent / "ledger", limit_usd="0.0000031")
    budget.admit("hello")
    parent.rename(tmp_path / "retained")
    parent.mkdir(mode=0o700)
    with pytest.raises(SpeechProviderError):
        budget.admit("hello")
    assert not (parent / "ledger").exists()
