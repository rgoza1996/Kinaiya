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
    InstallError,
    Layout,
    current_receipt,
    install_release,
    preflight,
    setup,
    source_artifact,
    update_plan,
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
    setup_parser = commands.add_parser(
        "setup", help="Configure Agent without starting work or voice"
    )
    setup_parser.add_argument("--yes", action="store_true")
    setup_parser.add_argument("--endpoint")
    setup_parser.add_argument("--model")
    setup_parser.add_argument("--credential-env")
    setup_parser.add_argument("--allow-cloud", action="store_true")
    setup_parser.add_argument("--converse", action="store_true")
    updater = commands.add_parser("update", help="Check or explicitly install a verified update")
    updater.add_argument("--channel", choices=("stable", "nightly"))
    updater.add_argument("--check", action="store_true")
    updater.add_argument(
        "--plan", action="store_true", help="Read-only update plan (same safety as --check)"
    )
    updater.add_argument("--yes", action="store_true")
    commands.add_parser("version", help="Verify and show installed immutable provenance")
    commands.add_parser("web", help="Serve matching prebuilt owner UI on loopback port 8780")
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


def chosen_layout():
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
        layout = Layout.for_home(Path.home())
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
            outgoing.flush()
            os.fsync(outgoing.fileno())
        verify_artifact(target, artifact)
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
    return target


def confirm(yes, message):
    print(message, flush=True)
    if yes:
        return
    try:
        with open_terminal() as tty:
            tty.write("Type yes to continue: ")
            if tty.readline().strip() == "yes":
                return
    except OSError:
        pass
    raise InstallError("Cancelled; use --yes only after reviewing the plan")


def dispatch(args):
    layout = chosen_layout()
    if args.command == "web":
        from services.distribution.web import serve_web

        serve_web(layout)
        return {"web": "stopped"}
    if args.command == "version":
        receipt = current_receipt(layout)
        return {
            "managed": receipt is not None,
            "release": receipt["release"] if receipt else None,
            "ready": "not_checked",
        }
    if args.command == "setup":
        if not args.yes:
            from services.distribution.wizard import run_wizard

            return run_wizard(layout)
        if not args.endpoint or not args.model:
            raise InstallError("Noninteractive setup requires --yes --endpoint and --model")
        return setup(
            layout,
            endpoint=args.endpoint,
            model=args.model,
            credential_env=args.credential_env,
            allow_cloud=args.allow_cloud,
            converse=args.converse,
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
    host = preflight(layout)
    plan = check_update(layout, args.channel)
    if plan["action"] == "current":
        return plan
    confirm(
        args.yes, json.dumps({"host": host, "plan": plan, "install": str(layout.install)}, indent=2)
    )
    layout.cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    with TemporaryDirectory(prefix="download-", dir=layout.cache) as temporary:
        artifact = source_artifact(plan["release"])
        archive = download_artifact(artifact, Path(temporary) / "source.tar.gz")
        install_release(layout, plan["release"], archive)
    result = {
        "installed": plan["release"],
        "ready": False,
        "launcher": str(layout.bin / "kinaiya"),
        "next": "kinaiya setup; kinaiya serve host",
    }
    if args.command == "install" and not args.no_setup and not layout.config.exists():
        import subprocess

        # The bootstrap interpreter intentionally lacks runtime dependencies.
        result["setup_exit_code"] = subprocess.run(
            [str(layout.bin / "kinaiya"), "setup"], check=False
        ).returncode
        if result["setup_exit_code"]:
            raise InstallError("Code installed; setup incomplete. Run kinaiya setup explicitly")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    configure(commands)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(dispatch(args), indent=2))
    except (InstallError, ReleaseError) as exc:
        parser.exit(1, f"kinaiya: {exc}\n")
    except (OSError, ValueError):
        parser.exit(
            1,
            "kinaiya: operation failed; preserve existing files and retry after checking permissions/network\n",
        )


if __name__ == "__main__":
    main()
