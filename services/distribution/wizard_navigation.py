"""Interactive Back/Cancel wizard navigation and targeted section reconfiguration."""

from __future__ import annotations

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
from services.distribution.installation import InstallError, Layout, read_json
from services.distribution.model_picker import (
    ProviderProfileChoice,
    SelectedModel,
    check_role_adapter_eligibility,
    discover_profile_models,
    format_profile_choices,
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
        profiles: Any = None,
        discover_models: bool = False,
        opener: Any = None,
    ) -> None:
        self.layout = layout
        self.tty = tty
        self.section = validate_setup_section(section)
        self.mode = mode
        self.mascot_fn = mascot_fn
        self.setup_fn = setup_fn
        self.reconfigure_fn = reconfigure_fn
        self.profile_id = profile_id
        self.getpass_fn = getpass_fn
        self.profiles = profiles
        self.discover_models = discover_models
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
        line = self.tty.readline()
        if not line:
            raise InstallError("Setup cancelled: terminal input closed")
        return line.strip()

    def _raw_yn_prompt(self, label: str, default: str = "no") -> str:
        default_bool = default.strip().lower() in {"y", "yes"}
        default_hint = "y" if default_bool else "n"
        while True:
            self.tty.write(f"{label} (y/n) [{default_hint}]: ")
            self.tty.flush()
            line = self.tty.readline()
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

    def _active_steps(self) -> list[_PromptStep]:
        steps: list[_PromptStep] = []
        converse_val = self.answers.get("converse", self.defaults.get("converse", "no"))
        converse_enabled = converse_val.strip().lower() == "yes"
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
        if key == "converse" and value.strip().lower() != "yes":
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
            disc = discover_profile_models(
                chosen_prof,
                allow_cloud_disclosed=allow_cloud,
                opener=self.opener,
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
        ep_recorded = (
            f"{adapter}={endpoint}" if adapter not in {"lmstudio", "meta"} else endpoint
        )
        self._record_answer("endpoint", ep_recorded)
        self._record_answer("model", sel.model_id)
        self._record_answer("allow_cloud", "yes" if allow_cloud else "no")
        self._record_answer("credential_env", credential_env or "")
        self.selected_agent_model = sel

    def run(self) -> dict[str, Any]:
        if self.mascot_fn is not None:
            cols = shutil.get_terminal_size().columns
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
                    "converse",
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

                if step.key == "credential_env":
                    workspace_val = self.answers.get("workspace", self.defaults["workspace"])
                    data_val = self.answers.get("data", self.defaults["data"])
                    chosen_tmp = replace(
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
                    chosen_tmp = replace(
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
                        persisted = load_stored_profiles(env_path=env_path)
                        if persisted:
                            self.tty.write(
                                format_profile_choices(persisted, role="agent", width=term_width)
                                + "\n"
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
                            picked_prof: ProviderProfileChoice | None = None
                            token_ep = raw_ep.strip().lower()
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
            chosen = replace(
                self.layout,
                workspace=Path(workspace).expanduser(),
                data=Path(data).expanduser(),
            )
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
                confirm_line = self.tty.readline()
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
        converse = self.answers.get("converse", self.defaults["converse"]).strip().lower() == "yes"
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
                "install": str(chosen.install),
                "launcher": str(chosen.bin / "kinaiya"),
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
