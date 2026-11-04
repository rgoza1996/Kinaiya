"""Dependency-free Linux installer/updater CLI; release policy lives in #262."""

import argparse
import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from packages.distribution.releases import (
    MAX_ASSET_BYTES,
    ReleaseError,
    open_release_url,
    resolve_release,
    verify_artifact,
)
from services.distribution.installation import (
    SUPPORTED_ROLE_ADAPTERS,
    InstallError,
    Layout,
    apt_simulation_command,
    current_receipt,
    dependency_install_command,
    install_missing_dependencies,
    install_release,
    load_recorded_layout,
    missing_system_dependencies,
    preflight,
    resolve_install_location,
    setup,
    source_artifact,
    update_plan,
)
from services.distribution.stage_progress import (
    StageReporter,
    activate_reporter,
    current_reporter,
)
from services.distribution.terminal import open_terminal


def configure(commands):
    install = commands.add_parser(
        "install", help="Verify and stage an official native Linux release"
    )
    install.add_argument("--channel", choices=("stable", "nightly"), default="stable")
    install.add_argument(
        "--yes", action="store_true", help="Confirm the displayed installation plan"
    )
    install.add_argument("--no-setup", action="store_true")
    install.add_argument(
        "--json", action="store_true", help="Emit deterministic machine-readable JSON output"
    )
    install.add_argument(
        "--install-dir",
        type=Path,
        help="Explicit target directory for managed Kinaiya install root",
    )
    install.add_argument(
        "--install-location",
        help="Install root selection: 'default', 'current', or an absolute directory path",
    )
    setup_parser = commands.add_parser(
        "setup", help="Configure Agent without starting work or voice"
    )
    setup_parser.add_argument(
        "section",
        nargs="?",
        help="Optional section to reconfigure: workspace, model, or converse",
    )
    setup_parser.add_argument("--yes", action="store_true")
    setup_parser.add_argument("--json", action="store_true")
    setup_mode = setup_parser.add_mutually_exclusive_group()
    setup_mode.add_argument(
        "--quick",
        action="store_true",
        help="Minimal first-run setup using safe default workspace and data paths",
    )
    setup_mode.add_argument(
        "--advanced",
        action="store_true",
        help="Full setup including custom host paths and per-role adapters",
    )
    setup_parser.add_argument("--endpoint")
    setup_parser.add_argument("--model")
    setup_parser.add_argument("--adapter", choices=SUPPORTED_ROLE_ADAPTERS["agent"], default=None)
    setup_parser.add_argument("--credential-env")
    setup_parser.add_argument("--allow-cloud", action="store_true")
    setup_parser.add_argument("--converse", action="store_true")
    setup_parser.add_argument(
        "--check-connection",
        action="store_true",
        help="Offer an opt-in bounded provider connection check after configuration is saved",
    )
    setup_parser.add_argument("--workspace", dest="setup_workspace", type=Path)
    setup_parser.add_argument("--data-dir", dest="setup_data_dir", type=Path)
    setup_parser.add_argument(
        "--converse-adapter", choices=SUPPORTED_ROLE_ADAPTERS["converse"], default=None
    )
    setup_parser.add_argument("--converse-endpoint", default=None)
    setup_parser.add_argument("--converse-model", default=None)
    setup_parser.add_argument("--converse-credential-env", default=None)
    setup_parser.add_argument("--converse-allow-cloud", action="store_true")
    setup_parser.add_argument(
        "--decisions-adapter", choices=SUPPORTED_ROLE_ADAPTERS["decisions"], default=None
    )
    setup_parser.add_argument("--decisions-endpoint", default=None)
    setup_parser.add_argument("--decisions-model", default=None)
    setup_parser.add_argument("--decisions-credential-env", default=None)
    setup_parser.add_argument("--decisions-allow-cloud", action="store_true")
    updater = commands.add_parser("update", help="Check or explicitly install a verified update")
    updater.add_argument("--channel", choices=("stable", "nightly"))
    updater.add_argument("--check", action="store_true")
    updater.add_argument(
        "--plan", action="store_true", help="Read-only update plan (same safety as --check)"
    )
    updater.add_argument("--yes", action="store_true")
    updater.add_argument(
        "--json", action="store_true", help="Emit deterministic machine-readable JSON output"
    )
    commands.add_parser("version", help="Verify and show installed immutable provenance")
    doctor = commands.add_parser(
        "doctor",
        help="Read-only local installation, configuration, and connection diagnostics",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit deterministic JSON diagnostics without ANSI escape codes",
    )
    doctor.add_argument(
        "--no-service-probe",
        action="store_true",
        help="Skip local loopback TCP listener probes",
    )
    web_parser = commands.add_parser(
        "web", help="Serve matching prebuilt owner UI on loopback port 8780"
    )
    web_parser.add_argument("--port", type=int, default=8780)
    start_parser = commands.add_parser(
        "start",
        help="Start configured local host services, matching web UI, and open the browser",
    )
    start_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not attempt to open a desktop browser after readiness",
    )
    start_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable startup status JSON",
    )
    start_parser.add_argument(
        "--web-port",
        type=int,
        default=8780,
        help="Loopback port for the prebuilt owner web UI (default: 8780)",
    )
    start_parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for local socket and health readiness (default: 15.0)",
    )
    recovery = commands.add_parser("recover", help="Reconcile an interrupted managed transaction")
    recovery.add_argument("--yes", action="store_true")
    rollback = commands.add_parser(
        "rollback", help="Restore retained code only; never restore databases"
    )
    rollback.add_argument("--generation", required=True)
    rollback.add_argument("--yes", action="store_true")
    uninstall = commands.add_parser(
        "uninstall", help="Remove managed application; retain user data by default"
    )
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--delete-data", action="store_true")
    uninstall.add_argument(
        "--confirm-data-deletion", help="Must be DELETE to authorize data/config/cache deletion"
    )
    auth_parser = commands.add_parser(
        "auth", help="Manage owner credentials without exposing secret values"
    )
    auth_subcommands = auth_parser.add_subparsers(dest="auth_command", required=True)
    rotate_owner = auth_subcommands.add_parser(
        "rotate-owner",
        help="Atomically rotate the owner bearer credential and revoke prior owner sessions",
    )
    rotate_owner.add_argument(
        "--yes", action="store_true", help="Confirm owner credential rotation and revocation"
    )
    rotate_owner.add_argument(
        "--data-dir",
        dest="auth_data_dir",
        type=Path,
        help="Private data directory containing service-tokens.json",
    )
    rotate_owner.add_argument(
        "--expected-epoch",
        dest="expected_owner_epoch",
        type=int,
        help="Require current owner_epoch to match before rotating",
    )


def chosen_layout(home: Path | None = None):
    home = home or Path.home()
    root = os.environ.get("KINAIYA_INSTALL_ROOT")
    if root:
        from services.distribution.installation import read_json

        saved = read_json(Path(root) / "layout.json")
        try:
            layout = Layout(**{key: Path(value) for key, value in saved.items()})
        except (TypeError, ValueError):
            raise InstallError(
                "Invalid managed layout; preserve existing files for recovery"
            ) from None
        if layout.install != Path(root):
            raise InstallError("Managed layout does not match launcher install root")
    else:
        recorded = load_recorded_layout(home)
        layout = recorded if recorded is not None else Layout.for_home(home)
    layout.validate()
    return layout


def check_update(layout, channel=None):
    """A check never creates directories, downloads artifacts or restarts services."""
    layout.validate()
    current = current_receipt(layout)
    selected = channel or (current["release"]["channel"] if current else "stable")
    target = resolve_release(selected)
    action = update_plan(current, target)
    return {
        "action": action,
        "channel": selected,
        "version": target["version"],
        "source_sha": target["source_sha"],
        "release": target,
        "state_policy": "no migrations; no database rollback",
        "services": "must be stopped explicitly; no task replay",
    }


def download_artifact(artifact, target):
    """HTTPS allowlist/redirect validation is shared with the release resolver."""
    if type(artifact.get("size")) is not int or not 0 < artifact["size"] <= MAX_ASSET_BYTES:
        raise InstallError("Invalid artifact size bound")
    reporter = current_reporter()
    created = False
    try:
        with target.open("xb") as outgoing:
            created = True
            started, total = time.monotonic(), 0
            with open_release_url(artifact["url"]) as response:
                while True:
                    if time.monotonic() - started > 600:
                        raise InstallError(
                            "Artifact download exceeded time bound; retry explicitly"
                        )
                    chunk = response.read(min(1024 * 1024, artifact["size"] - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > artifact["size"]:
                        raise InstallError("Artifact size exceeds release bound")
                    outgoing.write(chunk)
                    if reporter is not None:
                        reporter.update_download(total, artifact["size"])
            outgoing.flush()
            os.fsync(outgoing.fileno())
        verify_artifact(target, artifact)
        if reporter is not None:
            reporter.mark_sha256_verified(artifact.get("sha256"))
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return target


def confirm(yes, message, *, pre_consented=False):
    print(message, flush=True)
    if yes:
        return
    try:
        with open_terminal() as tty:
            tty.write("Continue? (y/n) [n]: ")
            if hasattr(tty, "flush"):
                tty.flush()
            line = tty.readline()
            if line.strip().lower() in {"y", "yes"} or (pre_consented and line == ""):
                return
    except OSError:
        pass
    raise InstallError("Cancelled; use --yes only after reviewing the plan")


def confirm_owner_rotation(yes, target_dir):
    if yes:
        return
    try:
        with open_terminal() as tty:
            tty.write(
                f"Rotate owner credential in {target_dir} and revoke active owner sessions? (y/n) [n]: "
            )
            tty.flush()
            if tty.readline().strip().lower() in {"y", "yes"}:
                return
    except OSError:
        pass
    raise InstallError("Cancelled; use --yes to confirm owner credential rotation")


def _invoking_directory() -> Path | None:
    env_pwd = os.environ.get("KINAIYA_INVOKING_PWD", "").strip()
    if env_pwd:
        return Path(env_pwd)
    if not os.environ.get("KINAIYA_INSTALL_ROOT"):
        return Path.cwd()
    return None


def _doctor_layout(args):
    from dataclasses import replace

    try:
        layout = chosen_layout()
    except InstallError:
        root = os.environ.get("KINAIYA_INSTALL_ROOT")
        layout = Layout.for_home(Path.home())
        if root:
            layout = replace(layout, install=Path(root))
    data_arg = getattr(args, "data_dir", None)
    config_arg = getattr(args, "config", None)
    workspace_arg = getattr(args, "workspace", None)
    if config_arg is not None:
        layout = replace(layout, config=Path(config_arg).resolve())
    if data_arg is not None and Path(data_arg) != Path("data"):
        layout = replace(layout, data=Path(data_arg).resolve())
    if workspace_arg is not None and Path(workspace_arg) != Path("workspace"):
        layout = replace(layout, workspace=Path(workspace_arg).resolve())
    return layout


def print_command_result(args, result):
    import shutil
    import sys

    if getattr(args, "command", None) == "start":
        return
    if getattr(args, "command", None) == "doctor":
        from services.distribution.doctor import format_doctor_report

        is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        if getattr(args, "json", False) or not is_tty:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            no_color = bool(os.environ.get("NO_COLOR"))
            width = shutil.get_terminal_size((80, 24)).columns
            print(
                format_doctor_report(result, no_color=no_color, is_tty=True, width=width),
                end="",
            )
        return
    print(json.dumps(result, indent=2))


def dispatch(args):
    if args.command == "auth":
        if getattr(args, "auth_command", None) != "rotate-owner":
            raise InstallError("Unsupported auth command")
        from services.common.auth import CredentialRotationError, rotate_owner_token

        target_dir = getattr(args, "auth_data_dir", None)
        if target_dir is None:
            launcher_data_dir = getattr(args, "data_dir", None)
            if launcher_data_dir is not None and (
                not os.environ.get("KINAIYA_INSTALL_ROOT") or launcher_data_dir != Path("data")
            ):
                target_dir = launcher_data_dir
            else:
                target_dir = chosen_layout().data
        confirm_owner_rotation(getattr(args, "yes", False), target_dir)
        try:
            return rotate_owner_token(
                target_dir,
                expected_owner_epoch=getattr(args, "expected_owner_epoch", None),
            )
        except (CredentialRotationError, PermissionError, FileNotFoundError, ValueError) as exc:
            raise InstallError(str(exc)) from None
    if args.command == "doctor":
        from services.distribution.doctor import run_doctor

        return run_doctor(
            _doctor_layout(args),
            probe_services=not getattr(args, "no_service_probe", False),
        )
    layout = chosen_layout()
    if args.command == "start":
        from services.distribution.start_supervisor import start_local

        return start_local(
            layout,
            no_browser=getattr(args, "no_browser", False),
            json_output=getattr(args, "json", False),
            web_port=getattr(args, "web_port", 8780),
            timeout=getattr(args, "timeout", 15.0),
        )
    if args.command == "web":
        from services.distribution.web import serve_web

        serve_web(layout, port=getattr(args, "port", 8780) or 8780)
        return {"web": "stopped"}
    if args.command == "version":
        receipt = current_receipt(layout)
        return {
            "managed": receipt is not None,
            "install": str(layout.install),
            "release": receipt["release"] if receipt else None,
            "ready": "not_checked",
        }
    if args.command == "setup":
        section = getattr(args, "section", None)
        if section is not None:
            from services.distribution.wizard_navigation import validate_setup_section

            section = validate_setup_section(section)
        if not args.yes:
            from services.distribution.wizard import run_wizard

            mode = (
                "quick"
                if getattr(args, "quick", False)
                else ("advanced" if getattr(args, "advanced", False) else None)
            )
            check_connection = bool(getattr(args, "check_connection", False))
            if section is not None:
                return run_wizard(layout, section=section, check_connection=check_connection)
            if check_connection:
                return (
                    run_wizard(layout, mode=mode, check_connection=True)
                    if mode is not None
                    else run_wizard(layout, check_connection=True)
                )
            return run_wizard(layout, mode=mode) if mode is not None else run_wizard(layout)
        if section is not None:
            from dataclasses import replace

            from services.distribution.installation import reconfigure_section

            target_layout = layout
            if getattr(args, "setup_workspace", None) is not None:
                target_layout = replace(
                    target_layout, workspace=Path(args.setup_workspace).expanduser()
                )
            if getattr(args, "setup_data_dir", None) is not None:
                target_layout = replace(target_layout, data=Path(args.setup_data_dir).expanduser())
            return reconfigure_section(
                target_layout,
                section,
                endpoint=args.endpoint,
                model=args.model,
                adapter=getattr(args, "adapter", None),
                credential_env=args.credential_env,
                allow_cloud=args.allow_cloud,
                converse=args.converse,
            )
        if not args.endpoint or not args.model:
            raise InstallError("Noninteractive setup requires --yes --endpoint and --model")
        from services.distribution.wizard_summary import sanitize_completion_receipt

        converse_adapter = getattr(args, "converse_adapter", None)
        converse_endpoint = getattr(args, "converse_endpoint", None)
        converse_model = getattr(args, "converse_model", None)
        converse_credential_env = getattr(args, "converse_credential_env", None)
        converse_allow_cloud = getattr(args, "converse_allow_cloud", False)
        decisions_adapter = getattr(args, "decisions_adapter", None)
        decisions_endpoint = getattr(args, "decisions_endpoint", None)
        decisions_model = getattr(args, "decisions_model", None)
        decisions_credential_env = getattr(args, "decisions_credential_env", None)
        decisions_allow_cloud = getattr(args, "decisions_allow_cloud", False)
        converse_profile = None
        if (
            converse_adapter is not None
            or converse_endpoint is not None
            or converse_model is not None
            or converse_credential_env is not None
            or converse_allow_cloud
        ):
            if not (converse_endpoint and converse_model):
                raise InstallError(
                    "Converse role requires both --converse-endpoint and --converse-model"
                )
            converse_profile = {
                "endpoint": converse_endpoint,
                "model": converse_model,
                "allow_cloud": bool(converse_allow_cloud),
                "credential_env": converse_credential_env,
            }
            if converse_adapter is not None:
                converse_profile["adapter"] = converse_adapter
        decisions_profile = None
        if (
            decisions_adapter is not None
            or decisions_endpoint is not None
            or decisions_model is not None
            or decisions_credential_env is not None
            or decisions_allow_cloud
        ):
            if not (decisions_endpoint and decisions_model):
                raise InstallError(
                    "Decisions role requires both --decisions-endpoint and --decisions-model"
                )
            decisions_profile = {
                "endpoint": decisions_endpoint,
                "model": decisions_model,
                "allow_cloud": bool(decisions_allow_cloud),
                "credential_env": decisions_credential_env,
            }
            if decisions_adapter is not None:
                decisions_profile["adapter"] = decisions_adapter
        raw = setup(
            layout,
            endpoint=args.endpoint,
            model=args.model,
            adapter=getattr(args, "adapter", None),
            credential_env=args.credential_env,
            allow_cloud=args.allow_cloud,
            converse=args.converse,
            converse_profile=converse_profile,
            decisions_profile=decisions_profile,
        )
        return sanitize_completion_receipt(
            {
                **(raw if isinstance(raw, dict) else {}),
                "install": str(layout.install),
                "launcher": str(layout.bin / "kinaiya"),
                "config": str(layout.config),
                "data": str(layout.data),
                "workspace": str(layout.workspace),
                "adapter": getattr(args, "adapter", None),
                "endpoint": args.endpoint,
                "model": args.model,
                "credential_env": args.credential_env,
                "allow_cloud": args.allow_cloud,
                "converse": bool(args.converse or converse_profile is not None),
                "converse_profile": converse_profile,
            }
        )
    if args.command in {"recover", "rollback", "uninstall"}:
        from services.distribution.installation import recover, rollback, uninstall

        confirm(args.yes, f"Planned {args.command}: {layout.install}; workspace is never deleted")
        if args.command == "recover":
            return recover(layout)
        if args.command == "rollback":
            return rollback(layout, args.generation)
        return uninstall(
            layout, delete_data=args.delete_data, confirmation=args.confirm_data_deletion
        )
    # Preflight is mandatory before *any* install/update mutation; --check/--plan
    # intentionally remain usable for read-only diagnosis on unqualified hosts.
    if args.command == "update" and (args.check or args.plan):
        return check_update(layout, args.channel)
    pre_consented = False
    if args.command == "install":
        install_dir = getattr(args, "install_dir", None)
        install_location = getattr(args, "install_location", None)
        home = layout.config.parent.parent
        invoking_dir = _invoking_directory()
        if install_dir is not None or install_location is not None or args.yes:
            layout, _ = resolve_install_location(
                home,
                base_layout=layout,
                install_dir=install_dir,
                install_location=install_location,
                invoking_dir=invoking_dir,
                interactive=False,
            )
        else:
            try:
                with open_terminal() as tty:
                    layout, pre_consented = resolve_install_location(
                        home,
                        base_layout=layout,
                        install_dir=install_dir,
                        install_location=install_location,
                        invoking_dir=invoking_dir,
                        interactive=True,
                        tty=tty,
                    )
            except OSError:
                raise InstallError(
                    "Interactive install requires a controlling terminal (/dev/tty) or --yes"
                ) from None
    reporter = StageReporter(
        log_path=layout.cache / "install.log",
        json_output=bool(getattr(args, "json", False)),
        defer_log_creation=True,
    )
    with activate_reporter(reporter):
        reporter.start_stage("preflight")
        try:
            try:
                host = preflight(layout, allow_missing_dependencies=(args.command == "install"))
                missing = (
                    list(host.get("missing_dependencies", []))
                    if isinstance(host, dict) and "missing_dependencies" in host
                    else (missing_system_dependencies() if args.command == "install" else [])
                )
            except TypeError:
                host = preflight(layout)
                missing = []
        except (InstallError, ReleaseError) as exc:
            reporter.fail_stage("preflight", str(exc))
            raise
        reporter.complete_stage("preflight")
        plan = check_update(layout, args.channel)
        if plan["action"] == "current" and not missing:
            return plan
        confirmation_payload = {"host": host, "plan": plan, "install": str(layout.install)}
        if missing:
            confirmation_payload["missing_dependencies"] = missing
            confirmation_payload["dependency_check"] = " ".join(apt_simulation_command(missing))
            confirmation_payload["dependency_command"] = " ".join(
                dependency_install_command(missing)
            )
            confirmation_payload["dependency_policy"] = (
                "solver-verified exact package=version install; refuses any upgrade, downgrade, or removal of existing packages"
            )
        try:
            confirm(
                args.yes,
                reporter.render_plan(confirmation_payload, indent=2),
                pre_consented=pre_consented,
            )
        except InstallError as exc:
            reporter.cancel(str(exc))
            raise
        layout.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        reporter.enable_log_persistence()
        reporter.start_stage("dependencies")
        try:
            if missing:
                install_missing_dependencies(missing)
                host = preflight(layout)
                reporter.complete_stage("dependencies", detail=f"installed {', '.join(missing)}")
            else:
                reporter.complete_stage("dependencies", detail="already satisfied")
        except (InstallError, ReleaseError) as exc:
            reporter.fail_stage("dependencies", str(exc))
            raise
        with TemporaryDirectory(prefix="download-", dir=layout.cache) as temporary:
            artifact = source_artifact(plan["release"])
            reporter.start_stage("download")
            try:
                archive = download_artifact(artifact, Path(temporary) / "source.tar.gz")
                reporter.complete_stage("download", sha256_verified=True)
            except (InstallError, ReleaseError) as exc:
                reporter.fail_stage("download", str(exc))
                raise
            reporter.start_stage("staging")
            try:
                install_release(layout, plan["release"], archive)
                reporter.complete_stage("staging")
            except (InstallError, ReleaseError) as exc:
                reporter.fail_stage("staging", str(exc))
                raise
        result = {
            "installed": plan["release"],
            "ready": False,
            "launcher": str(layout.bin / "kinaiya"),
            "log": str(reporter.log_path),
            "next": "kinaiya setup; kinaiya serve host",
        }
        if args.command == "install" and not args.no_setup and not layout.config.exists():
            import subprocess

            reporter.start_stage("setup_handoff")
            # The bootstrap interpreter intentionally lacks runtime dependencies.
            result["setup_exit_code"] = subprocess.run(
                [str(layout.bin / "kinaiya"), "setup"], check=False
            ).returncode
            if result["setup_exit_code"]:
                reporter.fail_stage(
                    "setup_handoff",
                    "Code installed; setup incomplete. Run kinaiya setup explicitly",
                )
                raise InstallError("Code installed; setup incomplete. Run kinaiya setup explicitly")
            reporter.complete_stage("setup_handoff")
        else:
            reporter.skip_stage("setup_handoff", reason="not required")
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    configure(commands)
    args = parser.parse_args(argv)
    try:
        print_command_result(args, dispatch(args))
    except (InstallError, ReleaseError) as exc:
        parser.exit(1, f"kinaiya: {exc}\n")
    except (OSError, ValueError):
        parser.exit(
            1,
            "kinaiya: operation failed; preserve existing files and retry after checking permissions/network\n",
        )


if __name__ == "__main__":
    main()
