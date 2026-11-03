"""Owner-driven terminal setup; no credential values, model downloads or inference.

ASCII adaptation of assets/mascot/kinaiya-mascot.png: swept hair, crescent/star,
robe and ripples. Original text adaptation under the repository Apache-2.0 license.
No runtime image dependency. The artwork's separate provenance is not established.
"""

from dataclasses import replace
from pathlib import Path

from services.distribution.installation import InstallError, check_outdated_system_blockers, setup
from services.distribution.mascot import MASCOT, render_mascot
from services.distribution.terminal import open_terminal

__all__ = ["MASCOT", "mascot", "run_wizard"]


def mascot(*, width=80, interactive=True):
    if not interactive:
        return "Kinaiya"
    if width < 16:
        return "Kinaiya"[: max(0, width)]
    return render_mascot(width=width, is_tty=True)


def run_wizard(layout, *, tty=None):
    """Use the controlling terminal, so piping the bootstrap cannot consume consent."""
    check_outdated_system_blockers(probe_subprocesses=False)
    if tty is None:
        try:
            with open_terminal() as terminal:
                return run_wizard(layout, tty=terminal)
        except OSError:
            raise InstallError(
                "No controlling terminal; use setup --yes with explicit endpoint/model"
            ) from None

    def ask(label, default=""):
        tty.write(f"{label}" + (f" [{default}]" if default else "") + ": ")
        tty.flush()
        answer = tty.readline()
        if not answer:
            raise InstallError("Setup cancelled: terminal input closed")
        return answer.strip() or default

    tty.write(render_mascot(tty=tty) + "\n")
    tty.write("Agent-only by default. No microphone capture or task execution.\n")
    tty.write("No models, voice packs or optional UI tools will be downloaded.\n")
    workspace = ask("Authorized workspace", str(layout.workspace))
    data = ask("Private data directory", str(layout.data))
    endpoint_input = ask(
        "Agent provider endpoint (or adapter=endpoint, e.g. lmstudio=http://127.0.0.1:1234)",
        "http://127.0.0.1:1234",
    )
    if "=" in endpoint_input and "://" not in endpoint_input.split("=", 1)[0]:
        adapter_raw, endpoint_raw = endpoint_input.split("=", 1)
        adapter: str | None = adapter_raw.strip()
        endpoint = endpoint_raw.strip()
    else:
        adapter = None
        endpoint = endpoint_input
    model = ask("Exact model name (required)")
    allow_cloud = (
        ask("Allow disclosure of authorized content to this cloud provider? yes/no", "no") == "yes"
    )
    credential_env = ask("Credential environment variable NAME only; never paste a key") or None
    converse = ask("Enable optional Converse text services? yes/no", "no") == "yes"
    converse_profile = None
    decisions_profile = None
    if converse:
        converse_adapter = ask("Converse provider adapter (lmstudio/compatible/meta)", "compatible")
        converse_endpoint = ask("Converse provider endpoint", endpoint)
        converse_model = ask("Exact Converse model name", model)
        converse_profile = {
            "kind": converse_adapter,
            "endpoint": converse_endpoint,
            "model": converse_model,
            "allow_cloud": allow_cloud,
            "credential_env": credential_env,
        }
        decisions_adapter = ask(
            "Decisions provider adapter (compatible/laya/native_d1/meta)", "compatible"
        )
        decisions_endpoint = ask("Decisions provider endpoint", converse_endpoint)
        decisions_model = ask("Exact Decisions model name", converse_model)
        decisions_profile = {
            "kind": decisions_adapter,
            "endpoint": decisions_endpoint,
            "model": decisions_model,
            "allow_cloud": allow_cloud,
            "credential_env": credential_env,
        }
    chosen = replace(layout, workspace=Path(workspace).expanduser(), data=Path(data).expanduser())
    tty.write(
        f"Planned config: {chosen.config}\nData: {chosen.data}\nWorkspace: {chosen.workspace}\n"
    )
    tty.write("Provider connection is not checked; saved configuration does not mean ready.\n")
    if ask("Write configuration and private service tokens? type yes", "no") != "yes":
        raise InstallError("Setup cancelled; no configuration written")
    setup_kwargs: dict = {
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
    return setup(chosen, **setup_kwargs)
