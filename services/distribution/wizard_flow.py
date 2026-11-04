"""First-run wizard flow coordinator with Quick Setup defaults and deferred advanced settings."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from services.distribution.credentials import (
    LocalCredentialsStore,
    apply_masked_key_action,
    default_providers_env_path,
    prompt_masked_api_key,
)
from services.distribution.installation import (
    InstallError,
    Layout,
    check_outdated_system_blockers,
    reconfigure_section,
    setup,
)
from services.distribution.terminal import open_terminal
from services.distribution.terminal_ui import (
    PALETTE,
    RESET,
    ColorMode,
    detect_color_mode,
)
from services.distribution.wizard_navigation import (
    WizardNavigator,
    is_cancel_command,
    validate_setup_section,
)

VALID_WIZARD_MODES = (None, "standard", "quick", "advanced")


def _is_local_endpoint(endpoint: str) -> bool:
    url = urlsplit(endpoint.strip())
    return url.hostname in {"localhost", "127.0.0.1", "::1"}


def ask_yn(tty, label: str, *, default: bool = False) -> bool:
    """Read an explicit boolean choice; closed input never accepts the default."""
    default_hint = "y" if default else "n"
    while True:
        tty.write(f"{label} (y/n) [{default_hint}]: ")
        tty.flush()
        answer = tty.readline()
        if not answer:
            raise InstallError("Setup cancelled: terminal input closed")
        cleaned = answer.strip().lower()
        if is_cancel_command(cleaned):
            raise InstallError("Setup cancelled; no configuration written")
        if not cleaned:
            return default
        if cleaned in {"y", "yes"}:
            return True
        if cleaned in {"n", "no"}:
            return False
        tty.write("Please enter y or n.\n")
        tty.flush()


def _parse_endpoint_input(endpoint_input: str) -> tuple[str | None, str]:
    if "=" in endpoint_input and "://" not in endpoint_input.split("=", 1)[0]:
        adapter_raw, endpoint_raw = endpoint_input.split("=", 1)
        return adapter_raw.strip() or None, endpoint_raw.strip()
    return None, endpoint_input.strip()


class WizardFlowCoordinator:
    """Coordinate Quick Setup and Advanced Setup terminal flows without starting work."""

    def __init__(
        self,
        layout: Layout,
        *,
        tty: Any,
        mode: str | None = None,
        section: str | None = None,
        mascot_fn: Callable[..., str] | None = None,
        setup_fn: Callable[..., dict[str, Any]] = setup,
        reconfigure_fn: Callable[..., dict[str, Any]] = reconfigure_section,
        profile_id: str = "p01",
        getpass_fn: Callable[..., str] | None = None,
        replace_fn: Callable[..., Any] = replace,
        profiles: Any = None,
        discover_models: bool = False,
        check_connection: bool = False,
        opener: Any = None,
    ) -> None:
        if mode not in VALID_WIZARD_MODES:
            raise InstallError(f"Unsupported wizard mode {mode!r}; select 'quick' or 'advanced'")
        self.layout = layout
        self.chosen_layout = layout
        self.used_quick = mode == "quick"
        self.tty = tty
        self.mode = mode
        self.section = validate_setup_section(section)
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

    def _paint(self, text: str, token_fg: str) -> str:
        if self.color_mode == ColorMode.NONE or not token_fg:
            return text
        return f"{token_fg}{text}{RESET}"

    def ask(self, label: str, default: str = "") -> str:
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
        answer = self.tty.readline()
        if not answer:
            raise InstallError("Setup cancelled: terminal input closed")
        cleaned = answer.strip()
        if is_cancel_command(cleaned):
            raise InstallError("Setup cancelled; no configuration written")
        return cleaned or default

    def ask_yn(self, label: str, *, default: bool = False) -> bool:
        return ask_yn(self.tty, label, default=default)

    def _write_header(self) -> None:
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

    def _collect_role_profiles(
        self,
        *,
        endpoint: str,
        model: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        converse_adapter = self.ask(
            "Converse provider adapter (lmstudio/compatible/meta)", "compatible"
        )
        converse_endpoint = self.ask("Converse provider endpoint", endpoint)
        converse_model = self.ask("Exact Converse model name", model)
        converse_allow_cloud = self.ask_yn(
            "Allow disclosure of authorized content to this Converse provider?"
        )
        converse_credential_env = (
            self.ask("Converse credential environment variable NAME only; never paste a key")
            or None
        )
        converse_profile = {
            "kind": converse_adapter,
            "endpoint": converse_endpoint,
            "model": converse_model,
            "allow_cloud": converse_allow_cloud,
            "credential_env": converse_credential_env,
        }
        decisions_adapter = self.ask(
            "Decisions provider adapter (compatible/laya/native_d1/meta)", "compatible"
        )
        decisions_endpoint = self.ask("Decisions provider endpoint", converse_endpoint)
        decisions_model = self.ask("Exact Decisions model name", converse_model)
        decisions_allow_cloud = self.ask_yn(
            "Allow disclosure of authorized content to this Decisions provider?"
        )
        decisions_credential_env = (
            self.ask("Decisions credential environment variable NAME only; never paste a key")
            or None
        )
        decisions_profile = {
            "kind": decisions_adapter,
            "endpoint": decisions_endpoint,
            "model": decisions_model,
            "allow_cloud": decisions_allow_cloud,
            "credential_env": decisions_credential_env,
        }
        return converse_profile, decisions_profile

    def _write_review_and_confirm(
        self,
        chosen: Layout,
        *,
        adapter: str | None,
        endpoint: str,
        model: str,
        allow_cloud: bool,
        converse: bool,
    ) -> None:
        effective_adapter = adapter or ("meta" if allow_cloud else "lmstudio")
        self.tty.write(
            f"Planned config: {chosen.config}\n"
            f"Data: {chosen.data}\n"
            f"Workspace: {chosen.workspace}\n"
            f"Agent provider: {effective_adapter} @ {endpoint} (model: {model})\n"
            f"Converse: {'enabled' if converse else 'disabled (Agent-only)'}\n"
        )
        self.tty.write(
            "Provider connection is not checked; saved configuration does not mean ready.\n"
        )
        if not self.ask_yn("Write configuration and private service tokens?"):
            raise InstallError("Setup cancelled; no configuration written")

    def _execute_setup(
        self,
        chosen: Layout,
        *,
        adapter: str | None,
        endpoint: str,
        model: str,
        allow_cloud: bool,
        credential_env: str | None,
        converse: bool,
        converse_profile: dict[str, Any] | None,
        decisions_profile: dict[str, Any] | None,
    ) -> dict[str, Any]:
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
        if chosen.config.exists():
            setup_kwargs["allow_existing"] = True
        raw_result = self.setup_fn(chosen, **setup_kwargs)
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
                credential_env=credential_env,
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

        effective_adapter = adapter or ("meta" if allow_cloud else "lmstudio")
        receipt = sanitize_completion_receipt(
            {
                **(raw_result if isinstance(raw_result, dict) else {}),
                "install": str(chosen.install),
                "launcher": str(chosen.bin / "kinaiya"),
                "config": str(chosen.config),
                "data": str(chosen.data),
                "workspace": str(chosen.workspace),
                "adapter": effective_adapter,
                "endpoint": endpoint,
                "model": model,
                "allow_cloud": allow_cloud,
                "credential_env": credential_env,
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

    def run_quick(self, *, skip_header: bool = False) -> dict[str, Any]:
        if not skip_header:
            self._write_header()
        self.tty.write(
            "Quick Setup: using safe default workspace and data paths "
            "(choose Advanced settings before confirmation to customize).\n"
        )
        workspace = str(self.layout.workspace)
        data = str(self.layout.data)
        endpoint_input = self.ask(
            "Agent provider endpoint (or adapter=endpoint, e.g. lmstudio=http://127.0.0.1:1234)",
            "http://127.0.0.1:1234",
        )
        adapter, endpoint = _parse_endpoint_input(endpoint_input)
        model = self.ask("Exact model name (required)")
        needs_cloud_prompts = not _is_local_endpoint(endpoint) or adapter == "meta"
        if needs_cloud_prompts:
            allow_cloud = self.ask_yn(
                "Allow disclosure of authorized content to this cloud provider?"
            )
            credential_env = (
                self.ask("Credential environment variable NAME only; never paste a key") or None
            )
        else:
            allow_cloud = False
            credential_env = None

        converse = self.ask_yn("Enable optional Converse text services?")
        open_advanced = self.ask_yn("Configure advanced host paths & per-role adapters?")
        converse_profile = None
        decisions_profile = None
        if open_advanced:
            workspace = self.ask("Authorized workspace", workspace)
            data = self.ask("Private data directory", data)
            if not needs_cloud_prompts:
                allow_cloud = self.ask_yn(
                    "Allow disclosure of authorized content to this cloud provider?"
                )
                credential_env = (
                    self.ask("Credential environment variable NAME only; never paste a key") or None
                )
            if converse:
                converse_profile, decisions_profile = self._collect_role_profiles(
                    endpoint=endpoint,
                    model=model,
                )

        chosen = self.replace_fn(
            self.layout,
            workspace=Path(workspace).expanduser(),
            data=Path(data).expanduser(),
        )
        self.chosen_layout = chosen
        self._write_review_and_confirm(
            chosen,
            adapter=adapter,
            endpoint=endpoint,
            model=model,
            allow_cloud=allow_cloud,
            converse=converse,
        )
        return self._execute_setup(
            chosen,
            adapter=adapter,
            endpoint=endpoint,
            model=model,
            allow_cloud=allow_cloud,
            credential_env=credential_env,
            converse=converse,
            converse_profile=converse_profile,
            decisions_profile=decisions_profile,
        )

    def run_advanced(self, *, first_workspace_answer: str | None = None) -> dict[str, Any]:
        if first_workspace_answer is None:
            self._write_header()
            workspace = self.ask("Authorized workspace", str(self.layout.workspace))
        else:
            workspace = first_workspace_answer
        data = self.ask("Private data directory", str(self.layout.data))
        endpoint_input = self.ask(
            "Agent provider endpoint (or adapter=endpoint, e.g. lmstudio=http://127.0.0.1:1234)",
            "http://127.0.0.1:1234",
        )
        adapter, endpoint = _parse_endpoint_input(endpoint_input)
        model = self.ask("Exact model name (required)")
        allow_cloud = self.ask_yn("Allow disclosure of authorized content to this cloud provider?")
        chosen = self.replace_fn(
            self.layout,
            workspace=Path(workspace).expanduser(),
            data=Path(data).expanduser(),
        )
        self.chosen_layout = chosen
        env_path = default_providers_env_path(chosen)
        existing_present = (
            LocalCredentialsStore(env_path).has_api_key(self.profile_id)
            if env_path.exists() and not env_path.is_symlink()
            else False
        )
        key_action = prompt_masked_api_key(
            self.tty,
            profile_id=self.profile_id,
            endpoint=endpoint,
            existing_present=existing_present,
            getpass_fn=self.getpass_fn,
            allow_bare_env_name=True,
        )
        credential_env = key_action.credential_env
        effective_agent_adapter = adapter or ("meta" if allow_cloud else "lmstudio")
        agent_credential_env = (
            None
            if (effective_agent_adapter == "lmstudio" and key_action.action in {"set", "keep"})
            else credential_env
        )
        converse = self.ask_yn("Enable optional Converse text services?")
        converse_profile = None
        decisions_profile = None
        if converse:
            converse_profile, decisions_profile = self._collect_role_profiles(
                endpoint=endpoint,
                model=model,
            )
        self._write_review_and_confirm(
            chosen,
            adapter=adapter,
            endpoint=endpoint,
            model=model,
            allow_cloud=allow_cloud,
            converse=converse,
        )
        result = self._execute_setup(
            chosen,
            adapter=adapter,
            endpoint=endpoint,
            model=model,
            allow_cloud=allow_cloud,
            credential_env=agent_credential_env,
            converse=converse,
            converse_profile=converse_profile,
            decisions_profile=decisions_profile,
        )
        if key_action.action in {"set", "remove", "keep"} and (
            key_action.action == "set" or env_path.exists()
        ):
            apply_masked_key_action(
                env_path,
                key_action,
                base_url=endpoint,
                selected_model=model,
            )
        return result

    def run(self) -> dict[str, Any]:
        if self.section is not None:
            return WizardNavigator(
                self.layout,
                tty=self.tty,
                section=self.section,
                mode=self.mode,
                mascot_fn=self.mascot_fn,
                setup_fn=self.setup_fn,
                reconfigure_fn=self.reconfigure_fn,
                profile_id=self.profile_id,
                getpass_fn=self.getpass_fn,
                replace_fn=self.replace_fn,
                profiles=self.profiles,
                discover_models=self.discover_models,
                check_connection=self.check_connection,
                opener=self.opener,
            ).run()
        if self.mode == "quick" and os.path.lexists(self.layout.config):
            raise InstallError("Preserving existing configuration; edit it explicitly instead")
        if self.mode == "quick":
            return self.run_quick()
        nav = WizardNavigator(
            self.layout,
            tty=self.tty,
            section=None,
            mode=self.mode,
            mascot_fn=self.mascot_fn,
            setup_fn=self.setup_fn,
            reconfigure_fn=self.reconfigure_fn,
            profile_id=self.profile_id,
            getpass_fn=self.getpass_fn,
            replace_fn=self.replace_fn,
            profiles=self.profiles,
            discover_models=self.discover_models,
            check_connection=self.check_connection,
            opener=self.opener,
        )
        res = nav.run()
        self.chosen_layout = nav.chosen_layout
        self.used_quick = nav.used_quick
        return res


def run_quick_wizard(
    layout: Layout,
    *,
    tty: Any = None,
    mascot_fn: Callable[..., str] | None = None,
    setup_fn: Callable[..., dict[str, Any]] = setup,
) -> dict[str, Any]:
    """Run the minimal first-run setup path with host paths and role overrides deferred."""
    check_outdated_system_blockers(probe_subprocesses=False)
    if tty is None:
        try:
            with open_terminal() as terminal:
                return run_quick_wizard(
                    layout,
                    tty=terminal,
                    mascot_fn=mascot_fn,
                    setup_fn=setup_fn,
                )
        except OSError:
            raise InstallError(
                "No controlling terminal; use setup --yes with explicit endpoint/model"
            ) from None
    coordinator = WizardFlowCoordinator(
        layout,
        tty=tty,
        mode="quick",
        mascot_fn=mascot_fn,
        setup_fn=setup_fn,
    )
    return coordinator.run()


run_first_run_wizard = run_quick_wizard
