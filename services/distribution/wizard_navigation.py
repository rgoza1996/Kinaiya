"""Interactive Back/Cancel wizard navigation and targeted section reconfiguration."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.distribution.installation import InstallError, Layout, read_json

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
        "decisions_adapter": "compatible",
        "decisions_endpoint": "",
        "decisions_model": "",
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

        decisions_cfg = providers.get("decisions")
        if isinstance(decisions_cfg, dict):
            if isinstance(decisions_cfg.get("kind"), str) and decisions_cfg["kind"]:
                defaults["decisions_adapter"] = decisions_cfg["kind"]
            if isinstance(decisions_cfg.get("base_url"), str) and decisions_cfg["base_url"]:
                defaults["decisions_endpoint"] = decisions_cfg["base_url"]
            d_model = decisions_cfg.get("model")
            if isinstance(d_model, str) and d_model and d_model != "configure-your-local-model":
                defaults["decisions_model"] = d_model

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
        "Allow disclosure of authorized content to this cloud provider? yes/no",
        "model",
    ),
    _PromptStep(
        "credential_env",
        "Credential environment variable NAME only; never paste a key",
        "model",
    ),
    _PromptStep("converse", "Enable optional Converse text services? yes/no", "converse"),
    _PromptStep(
        "converse_adapter",
        "Converse provider adapter (lmstudio/compatible/meta)",
        "converse",
    ),
    _PromptStep("converse_endpoint", "Converse provider endpoint", "converse"),
    _PromptStep("converse_model", "Exact Converse model name", "converse"),
    _PromptStep(
        "decisions_adapter",
        "Decisions provider adapter (compatible/laya/native_d1/meta)",
        "converse",
    ),
    _PromptStep("decisions_endpoint", "Decisions provider endpoint", "converse"),
    _PromptStep("decisions_model", "Exact Decisions model name", "converse"),
)

_CONVERSE_DEPENDENT_KEYS: tuple[str, ...] = (
    "converse_adapter",
    "converse_endpoint",
    "converse_model",
    "decisions_adapter",
    "decisions_endpoint",
    "decisions_model",
)


class WizardNavigator:
    """Interactive wizard state machine supporting Back/Cancel and section reconfiguration."""

    def __init__(
        self,
        layout: Layout,
        *,
        tty: Any,
        section: str | None = None,
        mascot_fn: Callable[..., str] | None = None,
        setup_fn: Callable[..., dict[str, Any]],
        reconfigure_fn: Callable[..., dict[str, Any]],
    ) -> None:
        self.layout = layout
        self.tty = tty
        self.section = validate_setup_section(section)
        self.mascot_fn = mascot_fn
        self.setup_fn = setup_fn
        self.reconfigure_fn = reconfigure_fn
        self.defaults = load_existing_setup_defaults(layout)
        self.answers: dict[str, str] = {}

    def _raw_prompt(self, label: str, default: str = "") -> str:
        self.tty.write(f"{label}" + (f" [{default}]" if default else "") + ": ")
        self.tty.flush()
        line = self.tty.readline()
        if not line:
            raise InstallError("Setup cancelled: terminal input closed")
        return line.strip()

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

    def run(self) -> dict[str, Any]:
        if self.mascot_fn is not None:
            self.tty.write(self.mascot_fn(width=shutil.get_terminal_size().columns) + "\n")
        self.tty.write("Agent-only by default. No microphone capture or task execution.\n")
        self.tty.write("No models, voice packs or optional UI tools will be downloaded.\n")

        index = 0
        while True:
            steps = self._active_steps()
            while index < len(steps):
                step = steps[index]
                default = self._default_for(step.key)
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
            confirm_raw = self._raw_prompt(
                "Write configuration and private service tokens? type yes", "no"
            )
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
                continue
            confirm_val = confirm_raw if confirm_raw else "no"
            if confirm_val.lower() not in {"yes", "y"}:
                raise InstallError("Setup cancelled; no configuration written")
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
                False
                if (_is_local_endpoint(converse_endpoint) and converse_adapter != "meta")
                else allow_cloud
            )
            converse_cred = None if converse_adapter == "lmstudio" else credential_env
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
                False
                if (
                    decisions_adapter in {"laya", "native_d1"}
                    or (_is_local_endpoint(decisions_endpoint) and decisions_adapter != "meta")
                )
                else allow_cloud
            )
            decisions_profile = {
                "kind": decisions_adapter,
                "endpoint": decisions_endpoint,
                "model": decisions_model,
                "allow_cloud": decisions_cloud,
                "credential_env": credential_env,
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

        setup_kwargs: dict[str, Any] = {
            "endpoint": endpoint,
            "model": model,
            "allow_cloud": allow_cloud,
            "credential_env": credential_env,
            "converse": converse,
        }
        if adapter is not None:
            setup_kwargs["adapter"] = adapter
        if converse_profile is not None:
            setup_kwargs["converse_profile"] = converse_profile
        if decisions_profile is not None:
            setup_kwargs["decisions_profile"] = decisions_profile
        return self.setup_fn(chosen, **setup_kwargs)
