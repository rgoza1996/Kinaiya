"""Owner-driven terminal setup; no credential values, model downloads or inference.

ASCII adaptation of assets/mascot/kinaiya-mascot.png: swept hair, crescent/star,
robe and ripples. Original text adaptation under the repository Apache-2.0 license.
No runtime image dependency. The artwork's separate provenance is not established.
"""

import shutil
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from services.distribution.installation import InstallError, check_outdated_system_blockers, setup
from services.distribution.terminal import open_terminal


def _is_loopback_endpoint(raw_endpoint: str) -> bool:
    return urlsplit(raw_endpoint).hostname in {"localhost", "127.0.0.1", "::1"}


MASCOT = r"""          .-.( * ).
     ~~~ / ~   .-`\
   ~ *  (____  / - )
    ~~~~~   \/\_ /
       __.-'/ /\\
   ___/   / /  \\___
        /_/ /\_\
       / / /  \ \
     _/ /_/    \_\
   ~~~(____)~~(____)~~~
       ~~~ Kinaiya ~~~"""


def mascot(*, width=80, interactive=True):
    if not interactive:
        return "Kinaiya"
    if width < max(map(len, MASCOT.splitlines())):
        return "Kinaiya"[: max(0, width)]
    return MASCOT


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

    tty.write(mascot(width=shutil.get_terminal_size().columns) + "\n")
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
        if converse_adapter == "meta" or not _is_loopback_endpoint(converse_endpoint):
            converse_allow_cloud = (
                ask(
                    "Allow disclosure of authorized Converse content to this cloud provider? yes/no",
                    "no",
                )
                == "yes"
            )
            converse_credential_env = (
                ask("Converse credential environment variable NAME only; never paste a key") or None
            )
        else:
            converse_allow_cloud = False
            converse_credential_env = (
                credential_env
                if (
                    not allow_cloud
                    and converse_endpoint == endpoint
                    and converse_adapter != "lmstudio"
                )
                else None
            )
        converse_profile = {
            "kind": converse_adapter,
            "endpoint": converse_endpoint,
            "model": converse_model,
            "allow_cloud": converse_allow_cloud,
            "credential_env": converse_credential_env,
        }
        decisions_adapter = ask(
            "Decisions provider adapter (compatible/laya/native_d1/meta)", "compatible"
        )
        decisions_endpoint = ask("Decisions provider endpoint", converse_endpoint)
        decisions_model = ask("Exact Decisions model name", converse_model)
        if decisions_adapter == "meta" or not _is_loopback_endpoint(decisions_endpoint):
            decisions_allow_cloud = (
                ask(
                    "Allow disclosure of authorized Decisions content to this cloud provider? yes/no",
                    "no",
                )
                == "yes"
            )
            decisions_credential_env = (
                ask("Decisions credential environment variable NAME only; never paste a key")
                or None
            )
        else:
            decisions_allow_cloud = False
            decisions_credential_env = (
                converse_credential_env
                if (not converse_allow_cloud and decisions_endpoint == converse_endpoint)
                else (
                    credential_env if (not allow_cloud and decisions_endpoint == endpoint) else None
                )
            )
        decisions_profile = {
            "kind": decisions_adapter,
            "endpoint": decisions_endpoint,
            "model": decisions_model,
            "allow_cloud": decisions_allow_cloud,
            "credential_env": decisions_credential_env,
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
