"""Interactive Back/Cancel wizard navigation and targeted section reconfiguration."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.distribution.credentials import (
    LocalCredentialsStore,
    MaskedKeyAction,
    apply_masked_key_action,
    default_providers_env_path,
    prompt_masked_api_key,
)
from services.distribution.curses_menu import (
    MenuCancelled,
    MenuGoBack,
    MenuItem,
    curses_radiolist,
    is_curses_available,
)
from services.distribution.installation import (
    PROTECTED_INSTALL_ROOTS,
    PROTECTED_INSTALL_TREES,
    InstallError,
    Layout,
    read_json,
)
from services.distribution.model_picker import (
    ProviderProfileChoice,
    SelectedModel,
    check_local_v1_hint,
    check_role_adapter_eligibility,
    discover_profile_models,
    format_profile_choices,
    get_starter_provider_presets,
    is_loopback_endpoint,
    load_stored_profiles,
    save_role_model_selection,
    select_model_interactively,
    select_profile_interactively,
)
from services.distribution.terminal_ui import (
    PALETTE,
    RESET,
    ColorMode,
    detect_color_mode,
)

VALID_SETUP_SECTIONS: tuple[str, ...] = ("workspace", "model", "converse")
BACK_COMMANDS: frozenset[str] = frozenset({":back", "<", "back"})
CONFIRM_BACK_COMMANDS: frozenset[str] = frozenset({":back", "<", "back", "b"})
CANCEL_COMMANDS: frozenset[str] = frozenset({":cancel", ":q", "cancel", "quit"})
_PROFILE_ID_RE = re.compile(r"^p([0-9]{2,6})$")


def _is_local_endpoint(endpoint: str) -> bool:
    url = urlsplit(endpoint.strip())
    return url.hostname in {"localhost", "127.0.0.1", "::1"}


def validate_setup_section(section: str | None) -> str | None:
    """Validate optional setup section name or raise an actionable InstallError."""
    if section is None:
        return None
    if not isinstance(section, str) or not section.strip():
        raise InstallError(
            f"Unsupported setup section {section!r}; valid sections: {', '.join(VALID_SETUP_SECTIONS)}"
        )
    normalized = section.strip().lower()
    if normalized not in VALID_SETUP_SECTIONS:
        raise InstallError(
            f"Unsupported setup section {section!r}; valid sections: {', '.join(VALID_SETUP_SECTIONS)}"
        )
    return normalized


def is_cancel_command(raw: str) -> bool:
    return raw.strip().lower() in CANCEL_COMMANDS


def is_back_command(raw: str, *, at_confirm: bool = False) -> bool:
    token = raw.strip().lower()
    if at_confirm and token.startswith(("back ", ":back ")):
        return True
    return token in (CONFIRM_BACK_COMMANDS if at_confirm else BACK_COMMANDS)


def validate_workspace_path(candidate: Path | str) -> Path:
    """Validate and normalize an authorized workspace root directory (#504).

    Ensures the path is non-empty, contains no control characters, expands '~',
    and refuses protected system roots.
    """
    raw = str(candidate).strip()
    if not raw:
        raise InstallError("Workspace path cannot be empty")
    if any(ord(c) < 32 for c in raw):
        raise InstallError("Workspace path must not contain control characters")
    expanded = Path(os.path.expanduser(raw))
    normalized = Path(os.path.abspath(expanded))
    posix_str = normalized.as_posix()
    if posix_str in PROTECTED_INSTALL_ROOTS or any(
        posix_str == tree or posix_str.startswith(tree + "/") for tree in PROTECTED_INSTALL_TREES
    ):
        raise InstallError(f"Refusing protected system root as workspace directory: {normalized}")
    return normalized


def validate_data_path(candidate: Path | str) -> Path:
    """Validate and normalize a private data root directory (#504)."""
    raw = str(candidate).strip()
    if not raw:
        raise InstallError("Private data directory path cannot be empty")
    if any(ord(c) < 32 for c in raw):
        raise InstallError("Private data directory path must not contain control characters")
    expanded = Path(os.path.expanduser(raw))
    normalized = Path(os.path.abspath(expanded))
    posix_str = normalized.as_posix()
    if posix_str in PROTECTED_INSTALL_ROOTS or any(
        posix_str == tree or posix_str.startswith(tree + "/") for tree in PROTECTED_INSTALL_TREES
    ):
        raise InstallError(f"Refusing protected system root as private data directory: {normalized}")
    return normalized


def parse_endpoint_input(endpoint_input: str) -> tuple[str | None, str]:
    if "=" in endpoint_input and "://" not in endpoint_input.split("=", 1)[0]:
        adapter_raw, endpoint_raw = endpoint_input.split("=", 1)
        return adapter_raw.strip() or None, endpoint_raw.strip()
    return None, endpoint_input.strip()


def load_existing_setup_defaults(layout: Layout) -> dict[str, Any]:
    """Load current values from an existing config.json if present for section defaults."""
    defaults: dict[str, Any] = {
        "workspace": str(layout.workspace),
        "data": str(layout.data),
        "endpoint": "http://127.0.0.1:1234",
        "model": "",
        "allow_cloud": "no",
        "credential_env": "",
        "converse": "no",
        "converse_adapter": "compatible",
        "converse_endpoint": "",
        "converse_model": "",
        "converse_allow_cloud": "no",
        "converse_credential_env": "",
        "decisions_adapter": "compatible",
        "decisions_endpoint": "",
        "decisions_model": "",
        "decisions_allow_cloud": "no",
        "decisions_credential_env": "",
    }
    if not layout.config.exists() or layout.config.is_symlink():
        return defaults
    try:
        config = read_json(layout.config)
    except InstallError:
        return defaults
    if not isinstance(config, dict):
        return defaults

    dist = config.get("distribution")
    if isinstance(dist, dict):
        if isinstance(dist.get("workspace"), str) and dist["workspace"]:
            defaults["workspace"] = dist["workspace"]
        if isinstance(dist.get("data_dir"), str) and dist["data_dir"]:
            defaults["data"] = dist["data_dir"]
        if isinstance(dist.get("converse"), bool):
            defaults["converse"] = "yes" if dist["converse"] else "no"

    providers = config.get("providers")
    if isinstance(providers, dict):
        agent = providers.get("agent")
        if isinstance(agent, dict):
            base_url = agent.get("base_url")
            kind = agent.get("kind")
            if isinstance(base_url, str) and base_url:
                if isinstance(kind, str) and kind and kind not in {"lmstudio", "meta"}:
                    defaults["endpoint"] = f"{kind}={base_url}"
                else:
                    defaults["endpoint"] = base_url
            model = agent.get("model")
            if isinstance(model, str) and model and model != "configure-your-local-model":
                defaults["model"] = model
            if isinstance(agent.get("allow_cloud"), bool):
                defaults["allow_cloud"] = "yes" if agent["allow_cloud"] else "no"
            api_key_env = agent.get("api_key_env")
            if isinstance(api_key_env, str):
                defaults["credential_env"] = api_key_env

        converse_cfg = providers.get("converse")
        if isinstance(converse_cfg, dict):
            if isinstance(converse_cfg.get("kind"), str) and converse_cfg["kind"]:
                defaults["converse_adapter"] = converse_cfg["kind"]
            if isinstance(converse_cfg.get("base_url"), str) and converse_cfg["base_url"]:
                defaults["converse_endpoint"] = converse_cfg["base_url"]
            c_model = converse_cfg.get("model")
            if isinstance(c_model, str) and c_model and c_model != "configure-your-local-model":
                defaults["converse_model"] = c_model
            if isinstance(converse_cfg.get("allow_cloud"), bool):
                defaults["converse_allow_cloud"] = "yes" if converse_cfg["allow_cloud"] else "no"
            if isinstance(converse_cfg.get("api_key_env"), str):
                defaults["converse_credential_env"] = converse_cfg["api_key_env"]

        decisions_cfg = providers.get("decisions")
        if isinstance(decisions_cfg, dict):
            if isinstance(decisions_cfg.get("kind"), str) and decisions_cfg["kind"]:
                defaults["decisions_adapter"] = decisions_cfg["kind"]
            if isinstance(decisions_cfg.get("base_url"), str) and decisions_cfg["base_url"]:
                defaults["decisions_endpoint"] = decisions_cfg["base_url"]
            d_model = decisions_cfg.get("model")
            if isinstance(d_model, str) and d_model and d_model != "configure-your-local-model":
                defaults["decisions_model"] = d_model
            if isinstance(decisions_cfg.get("allow_cloud"), bool):
                defaults["decisions_allow_cloud"] = "yes" if decisions_cfg["allow_cloud"] else "no"
            if isinstance(decisions_cfg.get("api_key_env"), str):
                defaults["decisions_credential_env"] = decisions_cfg["api_key_env"]

    return defaults


@dataclass(frozen=True)
class _PromptStep:
    key: str
    label: str
    section: str


_ALL_STEPS: tuple[_PromptStep, ...] = (
    _PromptStep("workspace", "Authorized workspace", "workspace"),
    _PromptStep("data", "Private data directory", "workspace"),
    _PromptStep(
        "endpoint",
        "Agent provider endpoint (or adapter=endpoint, e.g. lmstudio=http://127.0.0.1:1234)",
        "model",
    ),
    _PromptStep("model", "Exact model name (required)", "model"),
    _PromptStep(
        "allow_cloud",
        "Allow disclosure of authorized content to this cloud provider?",
        "model",
    ),
    _PromptStep(
        "credential_env",
        "Credential environment variable NAME only; never paste a key",
        "model",
    ),
    _PromptStep("converse", "Enable optional Converse text services?", "converse"),
    _PromptStep(
        "converse_adapter",
        "Converse provider adapter (lmstudio/compatible/meta)",
        "converse",
    ),
    _PromptStep("converse_endpoint", "Converse provider endpoint", "converse"),
    _PromptStep("converse_model", "Exact Converse model name", "converse"),
    _PromptStep(
        "converse_allow_cloud",
        "Allow disclosure of authorized content to this Converse provider?",
        "converse",
    ),
    _PromptStep(
        "converse_credential_env",
        "Converse credential environment variable NAME only; never paste a key",
        "converse",
    ),
    _PromptStep(
        "decisions_adapter",
        "Decisions provider adapter (compatible/laya/native_d1/meta)",
        "converse",
    ),
    _PromptStep("decisions_endpoint", "Decisions provider endpoint", "converse"),
    _PromptStep("decisions_model", "Exact Decisions model name", "converse"),
    _PromptStep(
        "decisions_allow_cloud",
        "Allow disclosure of authorized content to this Decisions provider?",
        "converse",
    ),
    _PromptStep(
        "decisions_credential_env",
        "Decisions credential environment variable NAME only; never paste a key",
        "converse",
    ),
)

_CONVERSE_DEPENDENT_KEYS: tuple[str, ...] = (
    "converse_adapter",
    "converse_endpoint",
    "converse_model",
    "converse_allow_cloud",
    "converse_credential_env",
    "decisions_adapter",
    "decisions_endpoint",
    "decisions_model",
    "decisions_allow_cloud",
    "decisions_credential_env",
)


class WizardNavigator:
    """Interactive wizard state machine supporting Back/Cancel and section reconfiguration."""

    def __init__(
        self,
        layout: Layout,
        *,
        tty: Any,
        section: str | None = None,
        mode: str | None = None,
        mascot_fn: Callable[..., str] | None = None,
        setup_fn: Callable[..., dict[str, Any]],
        reconfigure_fn: Callable[..., dict[str, Any]],
        profile_id: str = "p01",
        getpass_fn: Callable[..., str] | None = None,
        replace_fn: Callable[..., Any] = replace,
        profiles: Any = None,
        discover_models: bool = False,
        check_connection: bool = False,
        opener: Any = None,
    ) -> None:
        self.layout = layout
        self.chosen_layout = layout
        self.used_quick = mode == "quick"
        self.tty = tty
        self.section = validate_setup_section(section)
        self.mode = mode
        self.mascot_fn = mascot_fn
        self.setup_fn = setup_fn
        self.reconfigure_fn = reconfigure_fn
        self.profile_id = profile_id
        self.getpass_fn = getpass_fn
        self.replace_fn = replace_fn
        self.profiles = profiles
        self.discover_models = discover_models
        self.check_connection = check_connection
        self.opener = opener
        self.color_mode = detect_color_mode(tty)
        self.defaults = load_existing_setup_defaults(layout)
        self.answers: dict[str, str] = {}
        self.key_action: MaskedKeyAction | None = None
        self.selected_agent_model: SelectedModel | None = None

    def _paint(self, text: str, token_fg: str) -> str:
        if self.color_mode == ColorMode.NONE or not token_fg:
            return text
        return f"{token_fg}{text}{RESET}"

    def _raw_prompt(self, label: str, default: str = "") -> str:
        if self.color_mode == ColorMode.NONE:
            self.tty.write(f"{label}" + (f" [{default}]" if default else "") + ": ")
        else:
            c_lbl = PALETTE.moonlight.fg_sequence(self.color_mode)
            c_def = PALETTE.amber.fg_sequence(self.color_mode)
            prompt_str = f"{c_lbl}{label}{RESET}"
            if default:
                prompt_str += f" {c_def}[{default}]{RESET}"
            prompt_str += f"{c_lbl}: {RESET}"
            self.tty.write(prompt_str)
        self.tty.flush()
        try:
            line = self.tty.readline()
        except (KeyboardInterrupt, EOFError):
            self.tty.write("\n")
            self.tty.flush()
            raise InstallError("Setup cancelled; no configuration written") from None
        if not line:
            raise InstallError("Setup cancelled: terminal input closed")
        return line.strip()

    def _raw_yn_prompt(self, label: str, default: str = "no") -> str:
        default_bool = default.strip().lower() in {"y", "yes"}
        default_hint = "y" if default_bool else "n"
        while True:
            self.tty.write(f"{label} (y/n) [{default_hint}]: ")
            self.tty.flush()
            try:
                line = self.tty.readline()
            except (KeyboardInterrupt, EOFError):
                self.tty.write("\n")
                self.tty.flush()
                raise InstallError("Setup cancelled; no configuration written") from None
            if not line:
                raise InstallError("Setup cancelled: terminal input closed")
            cleaned = line.strip()
            if is_cancel_command(cleaned):
                raise InstallError("Setup cancelled; no configuration written")
            if is_back_command(cleaned, at_confirm=False):
                return cleaned
            if not cleaned:
                return "yes" if default_bool else "no"
            lowered = cleaned.lower()
            if lowered in {"y", "yes"}:
                return "yes"
            if lowered in {"n", "no"}:
                return "no"
            self.tty.write("Please enter y or n.\n")
            self.tty.flush()

    def _prompt_workspace_choice(self, default_val: str) -> str:
        cwd = os.environ.get("KINAIYA_INVOKING_PWD") or os.getcwd()
        default_ws = default_val or str(self.layout.workspace)

        if is_curses_available(self.tty):
            items = [
                MenuItem(
                    label=f"Default workspace ({default_ws})",
                    value=default_ws,
                    recommended=True,
                ),
                MenuItem(
                    label=f"Current working directory ({cwd})",
                    value=cwd,
                ),
                MenuItem(
                    label="Custom directory path...",
                    value=":custom",
                ),
            ]
            try:
                selected_idx = curses_radiolist(
                    "Select Authorized Workspace",
                    items,
                    default_index=0,
                    subtitle="Kinaiya tools and shell execution are strictly confined to this root.",
                    allow_back=False,
                    tty=self.tty,
                )
                chosen_val = items[selected_idx].effective_value
                if chosen_val == ":custom":
                    raw_custom = self._raw_prompt("Enter custom workspace path", default_ws)
                    return str(validate_workspace_path(raw_custom or default_ws))
                return str(validate_workspace_path(chosen_val))
            except MenuCancelled:
                raise InstallError("Setup cancelled; no configuration written") from None
            except MenuGoBack:
                return ":back"

        # Line mode fallback
        c_lbl = PALETTE.moonlight.fg_sequence(self.color_mode)
        c_em = PALETTE.emerald.fg_sequence(self.color_mode)
        pointer = self._paint("›", PALETTE.indigo.fg_sequence(self.color_mode)) if self.color_mode != ColorMode.NONE else ">"
        self.tty.write(f"\n{pointer} {c_lbl}Authorized Workspace Selection:{RESET}\n")
        self.tty.write(f"  [1] Default workspace ({default_ws}) {c_em}(recommended){RESET}\n")
        self.tty.write(f"  [2] Current working directory ({cwd})\n")
        self.tty.write(f"  [3] Custom directory path...\n")
        raw = self._raw_prompt("Authorized workspace [1-3] or enter path", default=default_ws)
        cleaned = raw.strip()
        if not cleaned or cleaned == "1" or cleaned == default_ws:
            return str(validate_workspace_path(default_ws))
        if cleaned == "2":
            return str(validate_workspace_path(cwd))
        if cleaned == "3":
            custom_path = self._raw_prompt("Enter custom workspace path", default=default_ws)
            return str(validate_workspace_path(custom_path or default_ws))
        if is_cancel_command(cleaned) or is_back_command(cleaned, at_confirm=False) or cleaned.lower() in {":quick", "quick"}:
            return cleaned
        return str(validate_workspace_path(cleaned))

    def _prompt_data_choice(self, default_val: str) -> str:
        default_data = default_val or str(self.layout.data)

        if is_curses_available(self.tty):
            items = [
                MenuItem(
                    label=f"Default private data directory ({default_data})",
                    value=default_data,
                    recommended=True,
                ),
                MenuItem(
                    label="Custom data directory path...",
                    value=":custom",
                ),
            ]
            try:
                selected_idx = curses_radiolist(
                    "Select Private Data Directory",
                    items,
                    default_index=0,
                    subtitle="Internal storage for journals, auth tokens, and session state.",
                    allow_back=True,
                    tty=self.tty,
                )
                chosen_val = items[selected_idx].effective_value
                if chosen_val == ":custom":
                    raw_custom = self._raw_prompt("Enter custom private data path", default_data)
                    return str(validate_data_path(raw_custom or default_data))
                return str(validate_data_path(chosen_val))
            except MenuCancelled:
                raise InstallError("Setup cancelled; no configuration written") from None
            except MenuGoBack:
                return ":back"

        # Line mode fallback
        c_lbl = PALETTE.moonlight.fg_sequence(self.color_mode)
        c_em = PALETTE.emerald.fg_sequence(self.color_mode)
        pointer = self._paint("›", PALETTE.indigo.fg_sequence(self.color_mode)) if self.color_mode != ColorMode.NONE else ">"
        self.tty.write(f"\n{pointer} {c_lbl}Private Data Directory Selection:{RESET}\n")
        self.tty.write(f"  [1] Default private data directory ({default_data}) {c_em}(recommended){RESET}\n")
        self.tty.write(f"  [2] Custom directory path...\n")
        raw = self._raw_prompt("Private data directory [1-2] or enter path", default=default_data)
        cleaned = raw.strip()
        if not cleaned or cleaned == "1" or cleaned == default_data:
            return str(validate_data_path(default_data))
        if cleaned == "2":
            custom_path = self._raw_prompt("Enter custom private data path", default=default_data)
            return str(validate_data_path(custom_path or default_data))
        if is_cancel_command(cleaned) or is_back_command(cleaned, at_confirm=False):
            return cleaned
        return str(validate_data_path(cleaned))

    def _prompt_custom_endpoint(self, default_val: str, profile_id: str = "p03") -> ProviderProfileChoice:
        """Prompt for custom OpenAI-compatible endpoint with /v1 check (#503)."""
        raw_url = self._raw_prompt(
            "Custom endpoint Base URL", default=default_val or "http://localhost:11434/v1"
        )
        if not raw_url:
            raw_url = default_val or "http://localhost:11434/v1"
        hint = check_local_v1_hint(raw_url)
        if hint is not None:
            self.tty.write(f"Hint: Local servers usually require /v1 suffix (e.g. {hint}).\n")
            fix = self._raw_yn_prompt(f"Add /v1 suffix to {raw_url}?", default="yes")
            if fix.strip().lower() in {"y", "yes"}:
                raw_url = hint
        is_local = is_loopback_endpoint(raw_url)
        if not is_local:
            cred_env = (
                self._raw_prompt(
                    "Credential environment variable NAME only (blank if none)", ""
                )
                or None
            )
            adapter = "meta"
            allow_cloud = True
        else:
            cred_env = None
            adapter = "lmstudio"
            allow_cloud = False
        pid = profile_id if _PROFILE_ID_RE.match(profile_id) else "p03"
        return ProviderProfileChoice(
            profile_id=pid,
            name="Custom OpenAI-compatible",
            base_url=raw_url,
            adapter_kind=adapter,
            allow_cloud=allow_cloud,
            credential_env=cred_env,
            models_path="/v1/models" if raw_url.rstrip("/").endswith("/v1") else "/models",
        )

    def _prompt_service_topology(self, default_val: str) -> str:
        """Prompt for 3-way service topology selection (#513, #505, #517)."""
        default_bool = default_val.strip().lower() in {"yes", "y", "both", "1"}
        default_hint = "both" if default_bool else "no"

        if is_curses_available(self.tty):
            items = [
                MenuItem(
                    label="Both Agent & Converse (Reuse Agent LLM)",
                    value="reuse",
                    description="Autonomous Agent + Converse dialogue (reuses Agent LLM) + independent Decisions router",
                    recommended=True,
                ),
                MenuItem(
                    label="Both Agent & Converse (Configure independently)",
                    value="yes",
                    description="Custom models for Agent, Converse dialogue, and Decisions router",
                ),
                MenuItem(
                    label="Agent only",
                    value="no",
                    description="Run autonomous coding Agent only; Converse and Decisions disabled",
                ),
                MenuItem(
                    label="Converse only",
                    value="converse_only",
                    description="Run conversational voice/dialogue character with Decisions timing without Agent",
                ),
            ]
            try:
                selected_idx = curses_radiolist(
                    "Service Topology Selection",
                    items,
                    default_index=0,
                    subtitle="Kinaiya supports Both Agent & Converse (default), Agent-only, and Converse-only.",
                    allow_back=True,
                    tty=self.tty,
                )
                return items[selected_idx].effective_value
            except MenuCancelled:
                raise InstallError("Setup cancelled; no configuration written") from None
            except MenuGoBack:
                return ":back"

        # Line-mode fallback
        c_lbl = PALETTE.moonlight.fg_sequence(self.color_mode)
        c_em = PALETTE.emerald.fg_sequence(self.color_mode)
        pointer = (
            self._paint("›", PALETTE.indigo.fg_sequence(self.color_mode))
            if self.color_mode != ColorMode.NONE
            else ">"
        )
        self.tty.write(f"\n{pointer} {c_lbl}Service Topology Selection:{RESET}\n")
        self.tty.write(
            f"  [1] Both Agent & Converse (reuse Agent LLM) {c_em}(recommended){RESET}\n"
        )
        self.tty.write("  [2] Both Agent & Converse (configure independently)\n")
        self.tty.write("  [3] Agent only\n")
        self.tty.write("  [4] Converse only\n")
        raw = self._raw_prompt(
            "Enable optional Converse text services? (y/n/reuse/1-4)",
            default=default_hint,
        )
        cleaned = raw.strip().lower()
        if not cleaned:
            return "yes" if default_bool else "no"
        if is_cancel_command(cleaned) or is_back_command(cleaned, at_confirm=False):
            return cleaned
        if cleaned in {"1", "reuse", "r"}:
            return "reuse"
        if cleaned in {"2", "yes", "y", "both"}:
            return "yes"
        if cleaned in {"3", "no", "n", "agent", "agent_only"}:
            return "no"
        if cleaned in {"4", "converse", "converse_only"}:
            return "converse_only"
        return cleaned

    def _apply_role_reuse_shortcut(self, default_val: str) -> None:
        """Apply fast-path role reuse for Converse while requiring independent Decisions model (#505, #517)."""
        self._record_answer("converse", "yes")
        endpoint_raw = self.answers.get("endpoint", self.defaults["endpoint"])
        adapter, endpoint = parse_endpoint_input(endpoint_raw)
        model = self.answers.get("model", self.defaults["model"])
        allow_cloud = self.answers.get("allow_cloud", self.defaults["allow_cloud"])
        cred_env = self.answers.get("credential_env", self.defaults["credential_env"])

        conv_adapter = "lmstudio" if adapter == "lmstudio" else "compatible"
        self._record_answer("converse_adapter", conv_adapter)
        self._record_answer("converse_endpoint", endpoint)
        self._record_answer("converse_model", model)
        self._record_answer("converse_allow_cloud", allow_cloud)
        self._record_answer("converse_credential_env", cred_env)

        # Decisions router model must be set independently (#517)
        self._record_answer("decisions_adapter", "compatible")
        self._record_answer("decisions_endpoint", endpoint)
        self._record_answer("decisions_allow_cloud", allow_cloud)
        self._record_answer("decisions_credential_env", cred_env)
        dec_model = self._raw_prompt(
            "Decisions router model name (independent model required, e.g. meta-llama/llama-3.2-3b-instruct)",
            default="meta-llama/llama-3.2-3b-instruct",
        )
        self._record_answer("decisions_model", dec_model or "meta-llama/llama-3.2-3b-instruct")

    def _active_steps(self) -> list[_PromptStep]:
        steps: list[_PromptStep] = []
        converse_val = self.answers.get("converse", self.defaults.get("converse", "no"))
        converse_enabled = converse_val.strip().lower() in {"yes", "converse_only"}
        for step in _ALL_STEPS:
            if self.section is not None and step.section != self.section:
                continue
            if step.key in _CONVERSE_DEPENDENT_KEYS and not converse_enabled:
                continue
            steps.append(step)
        return steps

    def _default_for(self, key: str) -> str:
        if key in self.answers:
            return self.answers[key]
        if key == "converse_endpoint":
            if "endpoint" in self.answers:
                _, ep = parse_endpoint_input(self.answers["endpoint"])
                return ep
            if self.defaults.get("converse_endpoint"):
                return self.defaults["converse_endpoint"]
            _, ep = parse_endpoint_input(self.defaults["endpoint"])
            return ep
        if key == "converse_model":
            if "model" in self.answers:
                return self.answers["model"]
            if self.defaults.get("converse_model"):
                return self.defaults["converse_model"]
            return self.defaults["model"]
        if key == "decisions_endpoint":
            if "converse_endpoint" in self.answers:
                return self.answers["converse_endpoint"]
            if self.defaults.get("decisions_endpoint"):
                return self.defaults["decisions_endpoint"]
            return self._default_for("converse_endpoint")
        if key == "decisions_model":
            if "converse_model" in self.answers:
                return self.answers["converse_model"]
            if self.defaults.get("decisions_model"):
                return self.defaults["decisions_model"]
            return self._default_for("converse_model")
        return str(self.defaults.get(key, ""))

    def _record_answer(self, key: str, value: str) -> None:
        previous = self.answers.get(key)
        self.answers[key] = value
        if key == "converse" and value.strip().lower() not in {"yes", "converse_only"}:
            # Discard any stale Converse/Decisions answers when Converse is disabled.
            for dep in _CONVERSE_DEPENDENT_KEYS:
                self.answers.pop(dep, None)
        elif key == "endpoint" and previous is not None and previous != value:
            self.answers.pop("converse_endpoint", None)
            self.answers.pop("decisions_endpoint", None)
        elif key == "model" and previous is not None and previous != value:
            self.answers.pop("converse_model", None)
            self.answers.pop("decisions_model", None)
        elif key == "converse_endpoint" and previous is not None and previous != value:
            self.answers.pop("decisions_endpoint", None)
        elif key == "converse_model" and previous is not None and previous != value:
            self.answers.pop("decisions_model", None)

    def _apply_chosen_profile(
        self,
        chosen_prof: ProviderProfileChoice,
        *,
        term_width: int,
    ) -> None:
        elig = check_role_adapter_eligibility("agent", chosen_prof.adapter_kind)
        if not elig.eligible:
            raise InstallError(elig.reason)
        adapter = chosen_prof.adapter_kind
        endpoint = chosen_prof.base_url
        allow_cloud = chosen_prof.allow_cloud
        credential_env = chosen_prof.credential_env
        if self.discover_models:
            from services.distribution.terminal_animation import BrandedActivityIndicator

            with BrandedActivityIndicator(
                self.tty,
                f"Discovering models for {chosen_prof.profile_id}",
                width=term_width,
            ) as indicator:
                indicator.tick("GET /models")
                disc = discover_profile_models(
                    chosen_prof,
                    allow_cloud_disclosed=allow_cloud,
                    opener=self.opener,
                )
                indicator.finish(
                    status=(
                        "pass"
                        if (disc.status == "ok" and not disc.manual_fallback_required)
                        else "fail"
                    ),
                    message=(
                        f"{len(disc.models)} models"
                        if (disc.status == "ok" and not disc.manual_fallback_required)
                        else "fallback to manual entry"
                    ),
                )
            sel = select_model_interactively(
                self.tty,
                disc,
                profile_id=chosen_prof.profile_id,
                role="agent",
                adapter_kind=adapter,
                endpoint=endpoint,
                credential_env=credential_env,
                allow_cloud=allow_cloud,
                width=term_width,
            )
        else:
            sel = select_model_interactively(
                self.tty,
                chosen_prof.discovered_models,
                profile_id=chosen_prof.profile_id,
                role="agent",
                adapter_kind=adapter,
                endpoint=endpoint,
                credential_env=credential_env,
                allow_cloud=allow_cloud,
                width=term_width,
            )
        ep_recorded = f"{adapter}={endpoint}" if adapter not in {"lmstudio", "meta"} else endpoint
        self._record_answer("endpoint", ep_recorded)
        self._record_answer("model", sel.model_id)
        self._record_answer("allow_cloud", "yes" if allow_cloud else "no")
        self._record_answer("credential_env", credential_env or "")
        self.selected_agent_model = sel

    def run(self) -> dict[str, Any]:
        if self.mascot_fn is not None:
            cols = shutil.get_terminal_size().columns
            from services.distribution.terminal_animation import (
                render_intro_sequence,
                should_animate,
            )

            if should_animate(self.tty, width=cols):
                from services.distribution.wizard import colorize_mascot

                render_intro_sequence(
                    self.tty,
                    width=cols,
                    colorize_fn=lambda frame: colorize_mascot(frame, mode=self.color_mode),
                )
            else:
                try:
                    banner = self.mascot_fn(width=cols, color=self.color_mode)
                except TypeError:
                    banner = self.mascot_fn(width=cols)
                self.tty.write(banner + "\n")
        c_slate = PALETTE.slate.fg_sequence(self.color_mode)
        self.tty.write(
            self._paint(
                "Agent-only by default. No microphone capture or task execution.",
                c_slate,
            )
            + "\n"
        )
        self.tty.write(
            self._paint(
                "No models, voice packs or optional UI tools will be downloaded.",
                c_slate,
            )
            + "\n"
        )

        index = 0
        while True:
            steps = self._active_steps()
            while index < len(steps):
                step = steps[index]
                default = self._default_for(step.key)
                if step.key in {
                    "allow_cloud",
                    "converse_allow_cloud",
                    "decisions_allow_cloud",
                }:
                    raw = self._raw_yn_prompt(step.label, default)
                    if is_back_command(raw, at_confirm=False):
                        if index > 0:
                            index -= 1
                        else:
                            self.tty.write("Already at the first step.\n")
                        steps = self._active_steps()
                        continue
                    self._record_answer(step.key, raw)
                    steps = self._active_steps()
                    index += 1
                    continue

                if step.key == "converse":
                    raw = self._prompt_service_topology(default)
                    if is_cancel_command(raw):
                        raise InstallError("Setup cancelled; no configuration written")
                    if is_back_command(raw, at_confirm=False):
                        if index > 0:
                            index -= 1
                        else:
                            self.tty.write("Already at the first step.\n")
                        steps = self._active_steps()
                        continue
                    if raw == "reuse":
                        self._apply_role_reuse_shortcut(default)
                        steps = self._active_steps()
                        index += len(_CONVERSE_DEPENDENT_KEYS) + 1
                        continue
                    self._record_answer(step.key, raw)
                    steps = self._active_steps()
                    index += 1
                    continue

                if step.key == "credential_env":
                    workspace_val = self.answers.get("workspace", self.defaults["workspace"])
                    data_val = self.answers.get("data", self.defaults["data"])
                    chosen_tmp = self.replace_fn(
                        self.layout,
                        workspace=Path(workspace_val).expanduser(),
                        data=Path(data_val).expanduser(),
                    )
                    env_path = default_providers_env_path(chosen_tmp)
                    existing_present = (
                        LocalCredentialsStore(env_path).has_api_key(self.profile_id)
                        if env_path.exists() and not env_path.is_symlink()
                        else False
                    )
                    _, ep_val = parse_endpoint_input(
                        self.answers.get("endpoint", self.defaults["endpoint"])
                    )
                    key_action = prompt_masked_api_key(
                        self.tty,
                        profile_id=self.profile_id,
                        endpoint=ep_val,
                        existing_present=existing_present,
                        getpass_fn=self.getpass_fn,
                        allow_bare_env_name=True,
                    )
                    if key_action.action == "back":
                        if index > 0:
                            index -= 1
                        else:
                            self.tty.write("Already at the first step.\n")
                        steps = self._active_steps()
                        continue
                    self.key_action = key_action
                    self._record_answer(step.key, key_action.credential_env or "")
                    steps = self._active_steps()
                    index += 1
                    continue

                if step.key == "endpoint":
                    term_width = shutil.get_terminal_size().columns
                    workspace_val = self.answers.get("workspace", self.defaults["workspace"])
                    data_val = self.answers.get("data", self.defaults["data"])
                    chosen_tmp = self.replace_fn(
                        self.layout,
                        workspace=Path(workspace_val).expanduser(),
                        data=Path(data_val).expanduser(),
                    )
                    env_path = default_providers_env_path(chosen_tmp)
                    if self.profiles is not None:
                        stored = load_stored_profiles(profiles=self.profiles)
                        if stored:
                            chosen_prof = select_profile_interactively(
                                self.tty, stored, role="agent", width=term_width
                            )
                            self._apply_chosen_profile(chosen_prof, term_width=term_width)
                            steps = self._active_steps()
                            index += 4
                            continue
                    else:
                        persisted = load_stored_profiles(
                            env_path=env_path,
                            include_starter_presets=(self.profiles is None),
                        )
                        if is_curses_available(self.tty):
                            menu_items = []
                            for p in persisted:
                                elig = p.eligibility_for("agent")
                                status_tag = "● Local" if not p.allow_cloud else "● Cloud"
                                menu_items.append(
                                    MenuItem(
                                        label=f"{p.name} ({status_tag})",
                                        value=p,
                                        description=f"{p.base_url} [{p.adapter_kind}]",
                                        recommended=(not p.allow_cloud),
                                        disabled=(not elig.eligible),
                                    )
                                )
                            menu_items.append(
                                MenuItem(
                                    label="Custom OpenAI-compatible endpoint (manual URL)...",
                                    value=":custom",
                                    description="Ollama, vLLM, llama.cpp, or remote API",
                                )
                            )
                            try:
                                selected_idx = curses_radiolist(
                                    "Select Provider",
                                    menu_items,
                                    default_index=0,
                                    subtitle="Choose a local or cloud LLM provider for the Agent.",
                                    allow_back=True,
                                    tty=self.tty,
                                )
                                chosen_item = menu_items[selected_idx].effective_value
                                if chosen_item == ":custom":
                                    custom_prof = self._prompt_custom_endpoint(
                                        default, profile_id=f"p{len(persisted) + 1:02d}"
                                    )
                                    self._apply_chosen_profile(custom_prof, term_width=term_width)
                                else:
                                    self._apply_chosen_profile(chosen_item, term_width=term_width)
                                steps = self._active_steps()
                                index += 4
                                continue
                            except MenuCancelled:
                                raise InstallError("Setup cancelled; no configuration written") from None
                            except MenuGoBack:
                                if index > 0:
                                    index -= 1
                                else:
                                    self.tty.write("Already at the first step.\n")
                                steps = self._active_steps()
                                continue
                        elif persisted:
                            self.tty.write(
                                format_profile_choices(persisted, role="agent", width=term_width)
                                + f"  {len(persisted) + 1}. Custom OpenAI-compatible endpoint (manual URL)...\n"
                            )
                            raw_ep = self._raw_prompt(step.label, default)
                            if is_cancel_command(raw_ep):
                                raise InstallError("Setup cancelled; no configuration written")
                            if is_back_command(raw_ep, at_confirm=False):
                                if index > 0:
                                    index -= 1
                                else:
                                    self.tty.write("Already at the first step.\n")
                                steps = self._active_steps()
                                continue
                            token_ep = raw_ep.strip().lower()
                            if token_ep in {str(len(persisted) + 1), ":custom", "custom"}:
                                custom_prof = self._prompt_custom_endpoint(
                                    default, profile_id=f"p{len(persisted) + 1:02d}"
                                )
                                self._apply_chosen_profile(custom_prof, term_width=term_width)
                                steps = self._active_steps()
                                index += 4
                                continue
                            picked_prof: ProviderProfileChoice | None = None
                            if token_ep.isdigit():
                                p_idx = int(token_ep)
                                if 1 <= p_idx <= len(persisted):
                                    picked_prof = persisted[p_idx - 1]
                            else:
                                for prof_cand in persisted:
                                    if prof_cand.profile_id.lower() == token_ep:
                                        picked_prof = prof_cand
                                        break
                            if picked_prof is not None:
                                self._apply_chosen_profile(picked_prof, term_width=term_width)
                                steps = self._active_steps()
                                index += 4
                                continue
                            resolved_ep = raw_ep if raw_ep else default
                            self._record_answer(step.key, resolved_ep)
                            steps = self._active_steps()
                            index += 1
                            continue

                if step.key == "workspace":
                    raw = self._prompt_workspace_choice(default)
                elif step.key == "data":
                    raw = self._prompt_data_choice(default)
                else:
                    raw = self._raw_prompt(step.label, default)
                if is_cancel_command(raw):
                    raise InstallError("Setup cancelled; no configuration written")
                if is_back_command(raw, at_confirm=False):
                    if index > 0:
                        index -= 1
                    else:
                        self.tty.write("Already at the first step.\n")
                    steps = self._active_steps()
                    continue
                if (
                    step.key == "workspace"
                    and index == 0
                    and self.section is None
                    and self.mode is None
                    and raw.strip().lower() in {":quick", "quick"}
                ):
                    from services.distribution.wizard_flow import WizardFlowCoordinator

                    self.used_quick = True
                    return WizardFlowCoordinator(
                        self.layout,
                        tty=self.tty,
                        mode="quick",
                        mascot_fn=None,
                        setup_fn=self.setup_fn,
                        reconfigure_fn=self.reconfigure_fn,
                        profile_id=self.profile_id,
                        getpass_fn=self.getpass_fn,
                    ).run_quick(skip_header=True)
                resolved = raw if raw else default
                self._record_answer(step.key, resolved)
                steps = self._active_steps()
                index += 1

            workspace = self.answers.get("workspace", self.defaults["workspace"])
            data = self.answers.get("data", self.defaults["data"])
            chosen = self.replace_fn(
                self.layout,
                workspace=Path(workspace).expanduser(),
                data=Path(data).expanduser(),
            )
            self.chosen_layout = chosen
            self.tty.write(
                f"Planned config: {chosen.config}\n"
                f"Data: {chosen.data}\n"
                f"Workspace: {chosen.workspace}\n"
            )
            self.tty.write(
                "Provider connection is not checked; saved configuration does not mean ready.\n"
            )
            while True:
                self.tty.write("Write configuration and private service tokens? (y/n) [n]: ")
                self.tty.flush()
                try:
                    confirm_line = self.tty.readline()
                except (KeyboardInterrupt, EOFError):
                    self.tty.write("\n")
                    self.tty.flush()
                    raise InstallError("Setup cancelled; no configuration written") from None
                if not confirm_line:
                    raise InstallError("Setup cancelled: terminal input closed")
                confirm_raw = confirm_line.strip()
                if is_cancel_command(confirm_raw):
                    raise InstallError("Setup cancelled; no configuration written")
                if is_back_command(confirm_raw, at_confirm=True):
                    token = confirm_raw.strip().lower()
                    steps = self._active_steps()
                    target_index = max(0, len(steps) - 1)
                    if " " in token:
                        _, target_name = token.split(" ", 1)
                        target_name = target_name.strip()
                        key_matches = [idx for idx, st in enumerate(steps) if st.key == target_name]
                        if key_matches:
                            target_index = key_matches[0]
                        else:
                            sec_matches = [
                                idx for idx, st in enumerate(steps) if st.section == target_name
                            ]
                            if sec_matches:
                                target_index = sec_matches[0]
                    index = target_index
                    rewound = True
                    break
                confirm_val = confirm_raw.lower() if confirm_raw else "no"
                if confirm_val in {"n", "no"}:
                    raise InstallError("Setup cancelled; no configuration written")
                if confirm_val in {"y", "yes"}:
                    rewound = False
                    break
                self.tty.write("Please enter y or n.\n")
                self.tty.flush()
            if rewound:
                continue
            break

        return self._commit(chosen)

    def _commit(self, chosen: Layout) -> dict[str, Any]:
        endpoint_input = self.answers.get("endpoint", self.defaults["endpoint"])
        adapter, endpoint = parse_endpoint_input(endpoint_input)
        model = self.answers.get("model", self.defaults["model"])
        allow_cloud = (
            self.answers.get("allow_cloud", self.defaults["allow_cloud"]).strip().lower() == "yes"
        )
        credential_env = (
            self.answers.get("credential_env", self.defaults["credential_env"]).strip() or None
        )
        converse_raw = self.answers.get("converse", self.defaults["converse"]).strip().lower()
        is_converse_only = converse_raw == "converse_only"
        converse = converse_raw in {"yes", "converse_only"}
        agent_enabled = not is_converse_only
        converse_profile: dict[str, Any] | None = None
        decisions_profile: dict[str, Any] | None = None
        if converse:
            converse_adapter = self.answers.get(
                "converse_adapter", self.defaults["converse_adapter"]
            )
            converse_endpoint = self.answers.get(
                "converse_endpoint", self._default_for("converse_endpoint")
            )
            converse_model = self.answers.get("converse_model", self._default_for("converse_model"))
            converse_cloud = (
                self.answers.get("converse_allow_cloud", self.defaults["converse_allow_cloud"])
                .strip()
                .lower()
                == "yes"
            )
            converse_cred = (
                self.answers.get(
                    "converse_credential_env", self.defaults["converse_credential_env"]
                ).strip()
                or None
            )
            converse_profile = {
                "kind": converse_adapter,
                "endpoint": converse_endpoint,
                "model": converse_model,
                "allow_cloud": converse_cloud,
                "credential_env": converse_cred,
            }
            decisions_adapter = self.answers.get(
                "decisions_adapter", self.defaults["decisions_adapter"]
            )
            decisions_endpoint = self.answers.get(
                "decisions_endpoint", self._default_for("decisions_endpoint")
            )
            decisions_model = self.answers.get(
                "decisions_model", self._default_for("decisions_model")
            )
            decisions_cloud = (
                self.answers.get("decisions_allow_cloud", self.defaults["decisions_allow_cloud"])
                .strip()
                .lower()
                == "yes"
            )
            decisions_cred = (
                self.answers.get(
                    "decisions_credential_env", self.defaults["decisions_credential_env"]
                ).strip()
                or None
            )
            decisions_profile = {
                "kind": decisions_adapter,
                "endpoint": decisions_endpoint,
                "model": decisions_model,
                "allow_cloud": decisions_cloud,
                "credential_env": decisions_cred,
            }

        if self.section is not None:
            return self.reconfigure_fn(
                chosen,
                self.section,
                endpoint=endpoint,
                model=model,
                adapter=adapter,
                allow_cloud=allow_cloud,
                credential_env=credential_env,
                converse=converse,
                agent=agent_enabled,
                converse_profile=converse_profile,
                decisions_profile=decisions_profile,
            )

        effective_agent_adapter = adapter or ("meta" if allow_cloud else "lmstudio")
        agent_credential_env = (
            None
            if (
                effective_agent_adapter == "lmstudio"
                and self.key_action is not None
                and self.key_action.action in {"set", "keep"}
            )
            else credential_env
        )
        setup_kwargs: dict[str, Any] = {
            "endpoint": endpoint,
            "model": model,
            "allow_cloud": allow_cloud,
            "credential_env": agent_credential_env,
            "converse": converse,
            "agent": agent_enabled,
        }
        if adapter is not None:
            setup_kwargs["adapter"] = adapter
        if converse_profile is not None:
            setup_kwargs["converse_profile"] = converse_profile
        if decisions_profile is not None:
            setup_kwargs["decisions_profile"] = decisions_profile
        if chosen.config.exists():
            setup_kwargs["allow_existing"] = True
        result = self.setup_fn(chosen, **setup_kwargs)
        env_path = default_providers_env_path(chosen)
        if (
            self.key_action is not None
            and self.key_action.action in {"set", "remove", "keep"}
            and (self.key_action.action == "set" or env_path.exists())
        ):
            apply_masked_key_action(
                env_path,
                self.key_action,
                base_url=endpoint,
                selected_model=model,
            )
        if self.selected_agent_model is not None and chosen.config.exists():
            save_role_model_selection(chosen.config, self.selected_agent_model)
        probe_dict: dict[str, Any] = {}
        if self.check_connection:
            from services.distribution.connection_probe import run_interactive_connection_check

            probe_res = run_interactive_connection_check(
                self.tty,
                endpoint=endpoint,
                model=model,
                role="agent",
                adapter_kind=adapter,
                allow_cloud=allow_cloud,
                credential_env=agent_credential_env,
                config_saved=True,
                opener=self.opener,
            )
            probe_dict = {
                "ready": probe_res.ready,
                "provider_connection": probe_res.provider_connection,
                "connection_detail": probe_res.overall_readiness,
                "connection_error": (
                    probe_res.message if probe_res.overall_readiness == "failed" else None
                ),
                "connection_probe": probe_res.to_dict(),
            }
        from services.distribution.wizard_summary import (
            render_completion_summary,
            sanitize_completion_receipt,
        )

        credential_saved = bool(
            self.key_action is not None and self.key_action.action in {"set", "keep"}
        )
        receipt = sanitize_completion_receipt(
            {
                **(result if isinstance(result, dict) else {}),
                "install": str(getattr(chosen, "install", "")),
                "launcher": (
                    str(Path(chosen.bin) / "kinaiya")
                    if getattr(chosen, "bin", None) is not None
                    else "kinaiya"
                ),
                "config": str(chosen.config),
                "data": str(chosen.data),
                "workspace": str(chosen.workspace),
                "adapter": effective_agent_adapter,
                "endpoint": endpoint,
                "model": model,
                "allow_cloud": allow_cloud,
                "credential_env": agent_credential_env,
                "credential_saved": credential_saved,
                "converse": converse,
                "converse_profile": converse_profile,
                **probe_dict,
            }
        )
        cols = shutil.get_terminal_size().columns
        self.tty.write(
            render_completion_summary(
                receipt,
                is_tty=True,
                no_color=bool(self.color_mode == ColorMode.NONE or cols < 40),
                width=cols,
            )
        )
        self.tty.flush()
        return receipt
