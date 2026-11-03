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
    setup,
)
from services.distribution.terminal import open_terminal

VALID_WIZARD_MODES = (None, "standard", "quick", "advanced")


def _is_local_endpoint(endpoint: str) -> bool:
    url = urlsplit(endpoint.strip())
    return url.hostname in {"localhost", "127.0.0.1", "::1"}


def _parse_yes_no(value: str, *, default: bool = False) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"y", "yes"}:
        return True
    if normalized in {"n", "no"}:
        return False
    return False


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
        mascot_fn: Callable[..., str] | None = None,
        setup_fn: Callable[..., dict[str, Any]] = setup,
        profile_id: str = "p01",
        getpass_fn: Callable[..., str] | None = None,
    ) -> None:
        if mode not in VALID_WIZARD_MODES:
            raise InstallError(f"Unsupported wizard mode {mode!r}; select 'quick' or 'advanced'")
        self.layout = layout
        self.tty = tty
        self.mode = mode
        self.mascot_fn = mascot_fn
        self.setup_fn = setup_fn
        self.profile_id = profile_id
        self.getpass_fn = getpass_fn

    def ask(self, label: str, default: str = "") -> str:
        self.tty.write(f"{label}" + (f" [{default}]" if default else "") + ": ")
        self.tty.flush()
        answer = self.tty.readline()
        if not answer:
            raise InstallError("Setup cancelled: terminal input closed")
        return answer.strip() or default

    def _write_header(self) -> None:
        if self.mascot_fn is not None:
            self.tty.write(self.mascot_fn(width=shutil.get_terminal_size().columns) + "\n")
        self.tty.write("Agent-only by default. No microphone capture or task execution.\n")
        self.tty.write("No models, voice packs or optional UI tools will be downloaded.\n")

    def _collect_role_profiles(
        self,
        *,
        endpoint: str,
        model: str,
        allow_cloud: bool,
        credential_env: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        converse_adapter = self.ask(
            "Converse provider adapter (lmstudio/compatible/meta)", "compatible"
        )
        converse_endpoint = self.ask("Converse provider endpoint", endpoint)
        converse_model = self.ask("Exact Converse model name", model)
        converse_profile = {
            "kind": converse_adapter,
            "endpoint": converse_endpoint,
            "model": converse_model,
            "allow_cloud": allow_cloud,
            "credential_env": credential_env,
        }
        decisions_adapter = self.ask(
            "Decisions provider adapter (compatible/laya/native_d1/meta)", "compatible"
        )
        decisions_endpoint = self.ask("Decisions provider endpoint", converse_endpoint)
        decisions_model = self.ask("Exact Decisions model name", converse_model)
        decisions_profile = {
            "kind": decisions_adapter,
            "endpoint": decisions_endpoint,
            "model": decisions_model,
            "allow_cloud": allow_cloud,
            "credential_env": credential_env,
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
        if not _parse_yes_no(
            self.ask("Write configuration and private service tokens? type yes", "no"),
            default=False,
        ):
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
        return self.setup_fn(chosen, **setup_kwargs)

    def run_quick(self) -> dict[str, Any]:
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
            allow_cloud = _parse_yes_no(
                self.ask(
                    "Allow disclosure of authorized content to this cloud provider? yes/no",
                    "no",
                ),
                default=False,
            )
            credential_env = (
                self.ask("Credential environment variable NAME only; never paste a key") or None
            )
        else:
            allow_cloud = False
            credential_env = None

        converse = _parse_yes_no(
            self.ask("Enable optional Converse text services? yes/no", "no"),
            default=False,
        )
        open_advanced = _parse_yes_no(
            self.ask("Configure advanced host paths & per-role adapters? (y/n)", "no"),
            default=False,
        )
        converse_profile = None
        decisions_profile = None
        if open_advanced:
            workspace = self.ask("Authorized workspace", workspace)
            data = self.ask("Private data directory", data)
            if not needs_cloud_prompts:
                allow_cloud = _parse_yes_no(
                    self.ask(
                        "Allow disclosure of authorized content to this cloud provider? yes/no",
                        "no",
                    ),
                    default=False,
                )
                credential_env = (
                    self.ask("Credential environment variable NAME only; never paste a key") or None
                )
            if converse:
                converse_profile, decisions_profile = self._collect_role_profiles(
                    endpoint=endpoint,
                    model=model,
                    allow_cloud=allow_cloud,
                    credential_env=credential_env,
                )

        chosen = replace(
            self.layout,
            workspace=Path(workspace).expanduser(),
            data=Path(data).expanduser(),
        )
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
        allow_cloud = _parse_yes_no(
            self.ask(
                "Allow disclosure of authorized content to this cloud provider? yes/no",
                "no",
            ),
            default=False,
        )
        chosen = replace(
            self.layout,
            workspace=Path(workspace).expanduser(),
            data=Path(data).expanduser(),
        )
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
        converse = _parse_yes_no(
            self.ask("Enable optional Converse text services? yes/no", "no"),
            default=False,
        )
        converse_profile = None
        decisions_profile = None
        if converse:
            converse_profile, decisions_profile = self._collect_role_profiles(
                endpoint=endpoint,
                model=model,
                allow_cloud=allow_cloud,
                credential_env=credential_env,
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
        if self.mode == "quick" and os.path.lexists(self.layout.config):
            raise InstallError("Preserving existing configuration; edit it explicitly instead")
        if self.mode == "quick":
            return self.run_quick()
        if self.mode == "advanced":
            return self.run_advanced()
        self._write_header()
        first = self.ask("Authorized workspace", str(self.layout.workspace))
        if first.strip().lower() in {":quick", "quick"}:
            return self.run_quick()
        return self.run_advanced(first_workspace_answer=first)


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
