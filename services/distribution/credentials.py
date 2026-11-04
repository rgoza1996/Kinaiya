"""User-private (0600) local .env credential persistence and masked TTY key entry.

IMPORTANT SECURITY NOTICE:
Credentials stored in `providers.env` are saved as user-private (`0600`) local
plaintext environment variables, NOT in an encrypted vault or hardware keychain.
Config files (`config.json`) store only the environment variable reference name
(e.g. `KINAIYA_PROVIDER_P01_API_KEY`), never the secret value itself.
"""

from __future__ import annotations

import fcntl
import io
import os
import re
import stat
import termios
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.distribution.installation import InstallError

MASKED_KEY_INDICATOR = "••••••••"
PLAINTEXT_STORAGE_NOTICE = (
    "Note: API keys are stored in user-private (0600) local plaintext .env "
    "(not an encrypted vault)."
)

_PROFILE_ID_RE = re.compile(r"^p([0-9]{2,6})$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STRICT_CREDENTIAL_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_BARE_ENV_VAR_HINT_RE = re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]+$")

_MANAGED_PROFILE_SUFFIXES = (
    "NAME",
    "BASE_URL",
    "API_KEY",
    "ENABLED",
    "MODEL",
    "DISCOVERED_MODELS",
    "ALLOW_REMOTE_HTTP",
    "CHAT_PATH",
    "MODELS_PATH",
    "CUSTOM_ENDPOINTS",
    "MAX_OUTPUT_TOKENS",
)


def normalize_profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str):
        raise InstallError("Provider profile ID must be a string (e.g. 'p01')")
    cleaned = profile_id.strip().lower()
    if not _PROFILE_ID_RE.match(cleaned):
        raise InstallError(f"Invalid provider profile ID {profile_id!r}; expected 'p01'..'p999999'")
    return cleaned


def profile_credential_env(profile_id: str) -> str:
    """Return the stable environment variable reference for a provider profile."""
    pid = normalize_profile_id(profile_id)
    return f"KINAIYA_PROVIDER_{pid.upper()}_API_KEY"


def default_providers_env_path(layout_or_dir: Any) -> Path:
    """Return the canonical user-private providers.env path for a Layout or directory."""
    if hasattr(layout_or_dir, "data"):
        return Path(layout_or_dir.data) / "providers.env"
    path = Path(layout_or_dir)
    if path.name.endswith(".env"):
        return path
    return path / "providers.env"


def mask_api_key(api_key: str | None) -> str:
    """Return a non-reversible masked indicator that never reveals the secret."""
    if not api_key:
        return ""
    return MASKED_KEY_INDICATOR


def _validate_no_control_chars(value: str, field_name: str, *, max_len: int = 512) -> str:
    if not isinstance(value, str):
        raise InstallError(f"{field_name} must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise InstallError(f"{field_name} must not contain control characters or newlines")
    cleaned = value.strip()
    if not cleaned:
        raise InstallError(f"{field_name} must not be empty")
    if len(cleaned) > max_len:
        raise InstallError(f"{field_name} exceeds maximum length ({max_len})")
    return cleaned


def _escape_dotenv_value(value: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise InstallError("Environment value must not contain control characters or newlines")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote_dotenv_value(raw_value: str) -> str:
    val = raw_value.strip()
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        inner = val[1:-1]
        out: list[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt in ('"', "\\"):
                    out.append(nxt)
                    i += 2
                    continue
            out.append(ch)
            i += 1
        return "".join(out)
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    for idx, ch in enumerate(val):
        if ch == "#" and (idx == 0 or val[idx - 1].isspace()):
            val = val[:idx].rstrip()
            break
    return val


@dataclass(frozen=True)
class _EnvLine:
    raw: str
    key: str | None = None
    value: str | None = None


def _parse_dotenv_lines(content: str) -> list[_EnvLine]:
    lines: list[_EnvLine] = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(_EnvLine(raw=raw_line))
            continue
        candidate = stripped
        if candidate.startswith("export "):
            candidate = candidate[len("export ") :].lstrip()
        if "=" not in candidate:
            lines.append(_EnvLine(raw=raw_line))
            continue
        key_part, val_part = candidate.split("=", 1)
        key = key_part.strip()
        if not _ENV_KEY_RE.match(key):
            lines.append(_EnvLine(raw=raw_line))
            continue
        lines.append(_EnvLine(raw=raw_line, key=key, value=_unquote_dotenv_value(val_part)))
    return lines


def _check_not_symlink(path: Path) -> None:
    abs_path = Path(os.path.abspath(path))
    if path.is_symlink() or abs_path.is_symlink():
        raise InstallError("Credential storage path cannot use symlink aliases")
    if abs_path.exists() and abs_path.resolve() != abs_path:
        raise InstallError("Credential storage path cannot traverse symlinked directories")
    if abs_path.parent.exists() and (
        abs_path.parent.is_symlink() or abs_path.parent.resolve() != abs_path.parent
    ):
        raise InstallError("Credential storage directory cannot use symlink aliases")


@contextmanager
def _credentials_lock(env_path: Path):
    _check_not_symlink(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_not_symlink(env_path.parent)
    lock_path = env_path.with_name(env_path.name + ".lock")
    _check_not_symlink(lock_path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError:
        raise InstallError("Refusing unsafe or inaccessible credential lock file") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise InstallError("Credential lock target must be a regular file")
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _read_dotenv_unlocked(env_path: Path) -> tuple[list[_EnvLine], dict[str, str]]:
    _check_not_symlink(env_path)
    if not os.path.lexists(env_path):
        return [], {}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(env_path, flags)
    except OSError:
        raise InstallError("Refusing symlinked or unreadable credential .env file") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise InstallError("Credential .env path must be a regular file")
        if st.st_size > 1024 * 1024:
            raise InstallError("Credential .env file exceeds size bound")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
            content = stream.read()
    finally:
        os.close(fd)
    lines = _parse_dotenv_lines(content)
    env_map: dict[str, str] = {}
    for item in lines:
        if item.key is not None and item.value is not None:
            env_map[item.key] = item.value
    return lines, env_map


def _write_dotenv_unlocked(env_path: Path, content: str) -> None:
    _check_not_symlink(env_path)
    env_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _check_not_symlink(env_path.parent)
    tmp_path = env_path.with_name(f".{env_path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp_path, flags, 0o600)
    except OSError:
        raise InstallError("Failed to create temporary credential file safely") from None
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, env_path)
        os.chmod(env_path, stat.S_IRUSR | stat.S_IWUSR)
    finally:
        tmp_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class StoredProfileCredential:
    profile_id: str
    name: str
    base_url: str
    credential_env: str
    has_api_key: bool
    enabled: bool = True
    selected_model: str = ""
    masked_api_key: str = ""


@dataclass(frozen=True)
class MaskedKeyAction:
    """Result of an interactive or programmatic credential prompt.

    `api_key` is excluded from `repr` so tracebacks and logs never leak secrets.
    """

    action: str  # "keep" | "skip" | "remove" | "env_ref" | "set"
    profile_id: str
    endpoint: str
    credential_env: str | None
    api_key: str | None = field(default=None, repr=False)
    masked_display: str = ""

    @property
    def credential_ref(self) -> str | None:
        return self.credential_env

    def __str__(self) -> str:
        return (
            f"MaskedKeyAction(action={self.action!r}, profile_id={self.profile_id!r}, "
            f"credential_env={self.credential_env!r}, masked_display={self.masked_display!r})"
        )


class LocalCredentialsStore:
    """User-private (0600) local .env store compatible with #352 custom provider schema.

    Secrets are persisted as plaintext environment variables in a 0600 file owned by
    the current user (not an encrypted vault).
    """

    def __init__(self, env_path: Path) -> None:
        _check_not_symlink(Path(env_path))
        self._env_path = Path(os.path.abspath(env_path))

    @property
    def env_path(self) -> Path:
        return self._env_path

    def load_env_map(self) -> dict[str, str]:
        with _credentials_lock(self._env_path):
            _, env_map = _read_dotenv_unlocked(self._env_path)
            return env_map

    def has_api_key(self, profile_id: str) -> bool:
        pid = normalize_profile_id(profile_id)
        env_map = self.load_env_map()
        return bool(env_map.get(profile_credential_env(pid), ""))

    def get_api_key(self, profile_id: str) -> str:
        pid = normalize_profile_id(profile_id)
        env_map = self.load_env_map()
        return env_map.get(profile_credential_env(pid), "")

    def get_profile(self, profile_id: str) -> StoredProfileCredential | None:
        pid = normalize_profile_id(profile_id)
        env_map = self.load_env_map()
        ids = [
            tok.strip().lower()
            for tok in env_map.get("KINAIYA_CUSTOM_PROVIDER_IDS", "").split(",")
            if tok.strip()
        ]
        prefix = f"KINAIYA_PROVIDER_{pid.upper()}_"
        if (
            pid not in ids
            and f"{prefix}BASE_URL" not in env_map
            and f"{prefix}API_KEY" not in env_map
        ):
            return None
        api_key = env_map.get(f"{prefix}API_KEY", "")
        enabled_raw = env_map.get(f"{prefix}ENABLED", "true").strip().lower()
        return StoredProfileCredential(
            profile_id=pid,
            name=env_map.get(f"{prefix}NAME", "") or pid.upper(),
            base_url=env_map.get(f"{prefix}BASE_URL", ""),
            credential_env=f"{prefix}API_KEY",
            has_api_key=bool(api_key),
            enabled=enabled_raw not in ("false", "0", "no", "off"),
            selected_model=env_map.get(f"{prefix}MODEL", ""),
            masked_api_key=mask_api_key(api_key),
        )

    def list_profiles(self) -> list[StoredProfileCredential]:
        env_map = self.load_env_map()
        raw_ids = env_map.get("KINAIYA_CUSTOM_PROVIDER_IDS", "")
        result: list[StoredProfileCredential] = []
        seen: set[str] = set()
        for tok in raw_ids.split(","):
            cand = tok.strip().lower()
            if not cand or not _PROFILE_ID_RE.match(cand) or cand in seen:
                continue
            seen.add(cand)
            prof = self.get_profile(cand)
            if prof is not None:
                result.append(prof)
        return result

    def set_profile_key(
        self,
        profile_id: str,
        api_key: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        enabled: bool | None = None,
        selected_model: str | None = None,
    ) -> StoredProfileCredential:
        pid = normalize_profile_id(profile_id)
        clean_key = _validate_no_control_chars(api_key, "API key", max_len=512)
        if clean_key == MASKED_KEY_INDICATOR:
            raise InstallError("Refusing to overwrite stored API key with masked placeholder")
        clean_name = (
            _validate_no_control_chars(name, "Provider name", max_len=96)
            if name is not None
            else None
        )
        clean_url = (
            _validate_no_control_chars(base_url, "Provider endpoint", max_len=256)
            if base_url is not None
            else None
        )
        clean_model = (
            _validate_no_control_chars(selected_model, "Model name", max_len=200)
            if selected_model is not None and selected_model.strip()
            else None
        )

        with _credentials_lock(self._env_path):
            lines, env_map = _read_dotenv_unlocked(self._env_path)
            self._mutate_profile_and_write(
                lines,
                env_map,
                profile_id=pid,
                api_key=clean_key,
                name=clean_name,
                base_url=clean_url,
                enabled=enabled,
                selected_model=clean_model,
            )
        prof = self.get_profile(pid)
        assert prof is not None
        return prof

    def remove_profile_key(
        self,
        profile_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
    ) -> StoredProfileCredential | None:
        pid = normalize_profile_id(profile_id)
        clean_name = (
            _validate_no_control_chars(name, "Provider name", max_len=96)
            if name is not None
            else None
        )
        clean_url = (
            _validate_no_control_chars(base_url, "Provider endpoint", max_len=256)
            if base_url is not None
            else None
        )
        with _credentials_lock(self._env_path):
            lines, env_map = _read_dotenv_unlocked(self._env_path)
            if not lines and not env_map:
                return None
            self._mutate_profile_and_write(
                lines,
                env_map,
                profile_id=pid,
                api_key="",
                name=clean_name,
                base_url=clean_url,
                enabled=None,
                selected_model=None,
                only_if_exists=(clean_name is None and clean_url is None),
            )
        return self.get_profile(pid)

    def _mutate_profile_and_write(
        self,
        lines: list[_EnvLine],
        env_map: dict[str, str],
        *,
        profile_id: str,
        api_key: str | None,
        name: str | None,
        base_url: str | None,
        enabled: bool | None,
        selected_model: str | None,
        only_if_exists: bool = False,
    ) -> None:
        pid = normalize_profile_id(profile_id)
        prefix = f"KINAIYA_PROVIDER_{pid.upper()}_"

        raw_ids = env_map.get("KINAIYA_CUSTOM_PROVIDER_IDS", "")
        profile_ids: list[str] = []
        seen: set[str] = set()
        max_seq = 0
        for tok in raw_ids.split(","):
            cand = tok.strip().lower()
            if not cand:
                continue
            m = _PROFILE_ID_RE.match(cand)
            if not m or cand in seen:
                continue
            seen.add(cand)
            profile_ids.append(cand)
            max_seq = max(max_seq, int(m.group(1)))

        for key in env_map:
            m = re.match(r"^KINAIYA_PROVIDER_P([0-9]{2,6})_", key)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        try:
            last_seq = int(env_map.get("KINAIYA_CUSTOM_PROVIDER_LAST_SEQ", "0").strip() or "0")
            max_seq = max(max_seq, last_seq)
        except ValueError:
            pass

        pid_num = int(_PROFILE_ID_RE.match(pid).group(1))  # type: ignore[union-attr]
        max_seq = max(max_seq, pid_num)

        if pid not in seen:
            if only_if_exists and f"{prefix}API_KEY" not in env_map:
                return
            profile_ids.append(pid)
            seen.add(pid)

        resolved_name = name or env_map.get(f"{prefix}NAME", "") or f"Profile {pid.upper()}"
        resolved_url = base_url or env_map.get(f"{prefix}BASE_URL", "") or "http://127.0.0.1:1234"
        resolved_key = env_map.get(f"{prefix}API_KEY", "") if api_key is None else api_key
        if enabled is None:
            enabled_raw = env_map.get(f"{prefix}ENABLED", "true").strip().lower()
            resolved_enabled = enabled_raw not in ("false", "0", "no", "off")
        else:
            resolved_enabled = bool(enabled)
        resolved_model = (
            selected_model if selected_model is not None else env_map.get(f"{prefix}MODEL", "")
        )

        target_updates: dict[str, str] = {
            "KINAIYA_CUSTOM_PROVIDER_IDS": ",".join(profile_ids),
            "KINAIYA_CUSTOM_PROVIDER_LAST_SEQ": str(max_seq),
            f"{prefix}NAME": _escape_dotenv_value(resolved_name),
            f"{prefix}BASE_URL": _escape_dotenv_value(resolved_url),
            f"{prefix}API_KEY": _escape_dotenv_value(resolved_key),
            f"{prefix}ENABLED": "true" if resolved_enabled else "false",
        }
        if resolved_model:
            target_updates[f"{prefix}MODEL"] = _escape_dotenv_value(resolved_model)

        written_keys: set[str] = set()
        output_lines: list[str] = []
        for item in lines:
            if item.key is not None and item.key in target_updates:
                if item.key not in written_keys:
                    output_lines.append(f"{item.key}={target_updates[item.key]}")
                    written_keys.add(item.key)
                continue
            output_lines.append(item.raw)

        ordered_Append_keys = [
            "KINAIYA_CUSTOM_PROVIDER_IDS",
            "KINAIYA_CUSTOM_PROVIDER_LAST_SEQ",
            f"{prefix}NAME",
            f"{prefix}BASE_URL",
            f"{prefix}API_KEY",
            f"{prefix}ENABLED",
            f"{prefix}MODEL",
        ]
        for key in ordered_Append_keys:
            if key in target_updates and key not in written_keys:
                output_lines.append(f"{key}={target_updates[key]}")
                written_keys.add(key)

        serialized = "\n".join(output_lines) + "\n"
        _write_dotenv_unlocked(self._env_path, serialized)


def apply_masked_key_action(
    env_path: Path,
    key_action: MaskedKeyAction,
    *,
    profile_name: str | None = None,
    base_url: str | None = None,
    selected_model: str | None = None,
) -> str | None:
    """Commit a MaskedKeyAction to the private local .env store after user confirmation."""
    store = LocalCredentialsStore(env_path)
    endpoint = base_url or key_action.endpoint
    if key_action.action == "set":
        if not key_action.api_key:
            raise InstallError("Cannot commit 'set' credential action without an API key")
        prof = store.set_profile_key(
            key_action.profile_id,
            key_action.api_key,
            name=profile_name,
            base_url=endpoint,
            selected_model=selected_model,
        )
        return prof.credential_env
    if key_action.action == "remove":
        store.remove_profile_key(
            key_action.profile_id,
            name=profile_name,
            base_url=endpoint,
        )
        return None
    if key_action.action == "keep":
        return key_action.credential_env
    if key_action.action == "env_ref":
        return key_action.credential_env
    if key_action.action == "skip":
        return None
    raise InstallError(f"Unsupported credential action {key_action.action!r}")


def _read_masked_line(
    tty, prompt_text: str, *, getpass_fn: Callable[[str], str] | None = None
) -> str:
    """Read a secret line without echoing characters to the terminal."""
    if getpass_fn is not None:
        tty.write(prompt_text)
        tty.flush()
        raw = getpass_fn("")
        tty.write("\n")
        tty.flush()
        return raw

    fileno: int | None = None
    if hasattr(tty, "fileno"):
        try:
            fd = tty.fileno()
            if isinstance(fd, int) and os.isatty(fd):
                fileno = fd
        except (OSError, ValueError, io.UnsupportedOperation):
            fileno = None

    if fileno is not None:
        tty.write(prompt_text)
        tty.flush()
        old_attrs = termios.tcgetattr(fileno)
        new_attrs = list(old_attrs)
        new_attrs[3] = new_attrs[3] & ~termios.ECHO
        try:
            termios.tcsetattr(fileno, termios.TCSADRAIN, new_attrs)
            line = tty.readline()
        finally:
            termios.tcsetattr(fileno, termios.TCSADRAIN, old_attrs)
            tty.write("\n")
            tty.flush()
        if not line:
            raise InstallError("Setup cancelled: terminal input closed")
        return line.rstrip("\r\n")

    tty.write(prompt_text)
    tty.flush()
    line = tty.readline()
    if not line:
        raise InstallError("Setup cancelled: terminal input closed")
    return line.rstrip("\r\n")


def prompt_masked_api_key(
    tty,
    *,
    profile_id: str = "p01",
    endpoint: str = "http://127.0.0.1:1234",
    existing_present: bool = False,
    getpass_fn: Callable[[str], str] | None = None,
    env_path: Path | None = None,
    profile_name: str | None = None,
    commit: bool = False,
    allow_bare_env_name: bool = False,
) -> MaskedKeyAction:
    """Prompt for an optional masked API key or credential reference without echoing secrets.

    - Blank Enter:
      - `existing_present=True` -> `action="keep"` (preserves existing key for `profile_id`)
      - `existing_present=False` -> `action="skip"` (no key needed, e.g. local server)
    - `:clear` or `:remove` -> `action="remove"` (removes stored key for `profile_id`)
    - `:env <VAR>` or `ENV:<VAR>` -> `action="env_ref"` (uses external env var name)
    - Secret string -> `action="set"` (stores in user-private 0600 plaintext `.env`)
    """
    pid = normalize_profile_id(profile_id)
    if env_path is not None and not existing_present:
        existing_present = LocalCredentialsStore(env_path).has_api_key(pid)

    cred_env_name = profile_credential_env(pid)
    default_hint = (
        "Enter to keep existing, :remove to clear, or :env VAR"
        if existing_present
        else "Enter to skip, paste masked key, or :env VAR"
    )
    prompt_label = (
        f"Provider API key for {pid} ({default_hint}; saved to 0600 plaintext .env, not a vault): "
    )

    raw_input = _read_masked_line(tty, prompt_label, getpass_fn=getpass_fn)
    cleaned = raw_input.strip()

    if not cleaned:
        if existing_present:
            tty.write(f"Credential for {pid}: [kept existing in private local .env]\n")
            tty.flush()
            result = MaskedKeyAction(
                action="keep",
                profile_id=pid,
                endpoint=endpoint,
                credential_env=cred_env_name,
                api_key=None,
                masked_display="[kept existing]",
            )
        else:
            result = MaskedKeyAction(
                action="skip",
                profile_id=pid,
                endpoint=endpoint,
                credential_env=None,
                api_key=None,
                masked_display="[none]",
            )
        if commit and env_path is not None:
            apply_masked_key_action(env_path, result, profile_name=profile_name, base_url=endpoint)
        return result

    lowered = cleaned.lower()
    if lowered in {":cancel", ":q", "cancel", "quit"}:
        raise InstallError("Setup cancelled; no configuration written")
    if lowered in {":back", "<", "back"}:
        return MaskedKeyAction(
            action="back",
            profile_id=pid,
            endpoint=endpoint,
            credential_env=None,
            api_key=None,
            masked_display="[back]",
        )
    if lowered in (":clear", ":remove"):
        tty.write(f"Credential for {pid}: [removed from private local .env]\n")
        tty.flush()
        result = MaskedKeyAction(
            action="remove",
            profile_id=pid,
            endpoint=endpoint,
            credential_env=None,
            api_key=None,
            masked_display="[removed]",
        )
        if commit and env_path is not None:
            apply_masked_key_action(env_path, result, profile_name=profile_name, base_url=endpoint)
        return result

    env_candidate: str | None = None
    if lowered == ":env":
        tty.write("Credential environment variable NAME only: ")
        tty.flush()
        env_line = tty.readline()
        if not env_line:
            raise InstallError("Setup cancelled: terminal input closed")
        env_candidate = env_line.strip()
    elif lowered.startswith(":env "):
        env_candidate = cleaned[5:].strip()
    elif cleaned.upper().startswith("ENV:"):
        env_candidate = cleaned[4:].strip()
    elif allow_bare_env_name and _BARE_ENV_VAR_HINT_RE.match(cleaned):
        env_candidate = cleaned

    if env_candidate is not None:
        if not _STRICT_CREDENTIAL_ENV_RE.match(env_candidate):
            raise InstallError(
                "Credentials accept an environment variable NAME only, never an API key"
            )
        tty.write(f"Credential for {pid}: referencing environment variable {env_candidate}\n")
        tty.flush()
        return MaskedKeyAction(
            action="env_ref",
            profile_id=pid,
            endpoint=endpoint,
            credential_env=env_candidate,
            api_key=None,
            masked_display=f"env:{env_candidate}",
        )

    secret_val = _validate_no_control_chars(cleaned, "API key", max_len=512)
    if secret_val == MASKED_KEY_INDICATOR or set(secret_val) == {"•"}:
        raise InstallError("Refusing masked placeholder as API key input")

    masked = mask_api_key(secret_val)
    tty.write(
        f"Credential for {pid}: {masked} [saved to private local .env (0600 plaintext, not an encrypted vault)]\n"
    )
    tty.flush()
    result = MaskedKeyAction(
        action="set",
        profile_id=pid,
        endpoint=endpoint,
        credential_env=cred_env_name,
        api_key=secret_val,
        masked_display=masked,
    )
    if commit and env_path is not None:
        apply_masked_key_action(env_path, result, profile_name=profile_name, base_url=endpoint)
    return result
