"""Terminal "Add custom API" form for first-run setup (#402).

Creates a named custom OpenAI-compatible provider profile through the existing
#352 ``CustomProvidersEnvStore`` guarded create operation, before any host
service runs. There is no second registry or secret store: the profile and its
optional key are written together by one locked, revisioned store mutation, so
cancellation or a failed save leaves neither an orphan credential nor a partial
profile.

Invariants:
- The draft stays in memory until the owner confirms it. Back/Cancel never write.
- The API key is read without echo on a real terminal, is never printed, and is
  never part of an error message or return value.
- Profile identity is the store's stable ``pNN`` ID, independent of the display
  name (duplicates are allowed and stay distinguishable) and of any model ID.
- No network, model probe, generation request, UI launch, or shell evaluation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from services.settings.custom_providers_env import (
    CustomProviderError,
    CustomProvidersEnvStore,
    normalize_openai_base_url,
)

BACK_TOKENS = frozenset({"<", ":back"})
CANCEL_TOKENS = frozenset({":cancel", ":q"})
MAX_NAME_CHARS = 96
NAV_HINT = "(type '<' to go back, ':cancel' to cancel)"

_STEPS = ("name", "base_url", "api_key", "confirm")


class ProfileFormCancelled(Exception):
    """The owner cancelled, or terminal input closed; nothing was written."""


@dataclass(frozen=True)
class CreatedProfile:
    profile_id: str
    name: str
    base_url: str


def custom_providers_store(layout, *, workspace: Path | None = None) -> CustomProvidersEnvStore:
    """Open the same ``<data>/custom_providers.env`` the host provider API uses."""
    return CustomProvidersEnvStore(
        Path(layout.data) / "custom_providers.env",
        workspace=Path(workspace if workspace is not None else layout.workspace),
    )


def _readline(tty, label: str) -> str:
    tty.write(label)
    tty.flush()
    try:
        answer = tty.readline()
    except (StopIteration, AssertionError):
        answer = ""
    if not answer:
        raise ProfileFormCancelled("Terminal input closed; no profile was saved.")
    return answer.rstrip("\r\n")


def masked_input(tty, label: str) -> str:
    """Read one line without echo when ``tty`` is a real terminal.

    Non-terminal streams (pipes, test fixtures) do not echo, so they are read
    directly. #389 owns the richer masked-entry contract; this is the minimal
    shared-safe fallback until that lands.
    """
    try:
        import termios

        fd = tty.fileno()
        if not tty.isatty():
            raise OSError
        previous = termios.tcgetattr(fd)
    except (ImportError, AttributeError, OSError, ValueError):
        return _readline(tty, label)
    hidden = list(previous)
    hidden[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, hidden)
        value = _readline(tty, label)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        tty.write("\n")
        tty.flush()
    return value


def confirm(tty, label: str, *, default: bool = False) -> bool | None:
    """y/n prompt; returns None when the owner asks to go back."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        answer = _readline(tty, label + suffix).strip().lower()
        if answer in CANCEL_TOKENS:
            raise ProfileFormCancelled("Cancelled; no profile was saved.")
        if answer in BACK_TOKENS:
            return None
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        tty.write("Please answer y or n.\n")


def run_add_custom_api(
    store: CustomProvidersEnvStore,
    tty,
    *,
    read_secret: Callable[[object, str], str] = masked_input,
) -> CreatedProfile:
    """Collect name, base URL and optional key, then create one profile on confirm."""
    name = base_url = normalized = ""
    api_key = ""
    key_entered = False
    operation_id = f"terminal-add-{uuid.uuid4().hex}"
    step = 0
    tty.write(f"Add custom API {NAV_HINT}\n")
    while True:
        current = _STEPS[step]
        if current == "name":
            label = "Display name" + (f" [{name}]" if name else "") + ": "
            answer = _readline(tty, label).strip()
            if answer in CANCEL_TOKENS:
                raise ProfileFormCancelled("Cancelled; no profile was saved.")
            if answer in BACK_TOKENS:
                raise ProfileFormCancelled("Back from the first field; no profile was saved.")
            answer = answer or name
            if not answer:
                tty.write("A display name is required.\n")
                continue
            if len(answer) > MAX_NAME_CHARS:
                tty.write(f"Display name must be at most {MAX_NAME_CHARS} characters.\n")
                continue
            name, step = answer, step + 1
        elif current == "base_url":
            hint = base_url or "http://127.0.0.1:1234/v1"
            answer = _readline(tty, f"Base URL [{hint}]: ").strip()
            if answer in CANCEL_TOKENS:
                raise ProfileFormCancelled("Cancelled; no profile was saved.")
            if answer in BACK_TOKENS:
                step -= 1
                continue
            candidate = answer or base_url
            if not candidate:
                tty.write("A base URL is required.\n")
                continue
            try:
                normalized = normalize_openai_base_url(candidate)
            except CustomProviderError as exc:
                tty.write(f"{exc}\n")
                continue
            base_url, step = candidate, step + 1
        elif current == "api_key":
            kept = (
                " (Enter keeps the key you typed)" if key_entered else " (optional; Enter to skip)"
            )
            answer = read_secret(tty, f"API key{kept}: ")
            stripped = answer.strip()
            if stripped in CANCEL_TOKENS:
                raise ProfileFormCancelled("Cancelled; no profile was saved.")
            if stripped in BACK_TOKENS:
                step -= 1
                continue
            if any(ch in answer for ch in ("\x00", "\r", "\n")):
                tty.write("The API key must be a single line.\n")
                continue
            if stripped:
                api_key, key_entered = stripped, True
            step += 1
        else:
            tty.write(
                f"Name: {name}\nBase URL: {normalized}\n"
                f"API key: {'set (hidden)' if api_key else 'none'}\n"
                "Saving creates the profile only; it does not contact the provider.\n"
            )
            decision = confirm(tty, "Save this custom API profile?")
            if decision is None:
                step -= 1
                continue
            if not decision:
                raise ProfileFormCancelled("Not saved; no profile was written.")
            try:
                snapshot = store.add_profile(
                    name=name,
                    base_url=base_url,
                    api_key=api_key,
                    client_operation_id=operation_id,
                )
            except (CustomProviderError, OSError) as exc:
                # Store messages name fields only, never secret values. The same
                # operation ID makes an uncertain retry replay, not duplicate.
                message = str(exc) if isinstance(exc, CustomProviderError) else "storage error"
                tty.write(
                    f"Could not save the profile: {message}\nCorrect the answers and retry.\n"
                )
                step = 0
                continue
            profile_id = snapshot["created_profile_id"]
            tty.write(f"Saved custom API profile {profile_id} ({name}).\n")
            return CreatedProfile(profile_id=profile_id, name=name, base_url=normalized)


def choose_or_add_profile(
    store: CustomProvidersEnvStore,
    tty,
    *,
    read_secret: Callable[[object, str], str] = masked_input,
) -> str:
    """Return a stable profile ID: add one when none exist, else choose or add."""
    profiles = store.snapshot()["profiles"]
    if not profiles:
        return run_add_custom_api(store, tty, read_secret=read_secret).profile_id
    while True:
        tty.write("Custom API profiles:\n")
        for index, profile in enumerate(profiles, 1):
            state = "" if profile["enabled"] else " (disabled)"
            tty.write(
                f"  {index}) {profile['name']} [{profile['profile_id']}] "
                f"{profile['base_url']}{state}\n"
            )
        tty.write("  a) Add custom API\n")
        answer = _readline(tty, "Choose a profile number or 'a': ").strip().lower()
        if answer in CANCEL_TOKENS:
            raise ProfileFormCancelled("Cancelled; no profile was chosen.")
        if answer == "a":
            try:
                return run_add_custom_api(store, tty, read_secret=read_secret).profile_id
            except ProfileFormCancelled as exc:
                if "Back from the first field" not in str(exc):
                    raise
                continue
        if answer.isdigit() and 1 <= int(answer) <= len(profiles):
            return profiles[int(answer) - 1]["profile_id"]
        tty.write("Please choose a listed number or 'a'.\n")
