"""Owner-driven terminal setup; no credential values, model downloads or inference.

ASCII adaptation of assets/mascot/kinaiya-mascot.png: swept hair, crescent/star,
robe and ripples. Original text adaptation under the repository Apache-2.0 license.
No runtime image dependency. The artwork's separate provenance is not established.
"""

import shutil
from dataclasses import replace
from pathlib import Path

from services.distribution.installation import InstallError, check_outdated_system_blockers, setup
from services.distribution.terminal import open_terminal

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


def run_wizard(layout, *, tty=None, store_factory=None, read_secret=None):
    """Use the controlling terminal, so piping the bootstrap cannot consume consent."""
    check_outdated_system_blockers(probe_subprocesses=False)
    if tty is None:
        try:
            with open_terminal() as terminal:
                return run_wizard(
                    layout, tty=terminal, store_factory=store_factory, read_secret=read_secret
                )
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
    endpoint = ask("OpenAI-compatible provider endpoint", "http://127.0.0.1:8080/v1")
    model = ask("Exact model name (required)")
    allow_cloud = (
        ask("Allow disclosure of authorized content to this cloud provider? yes/no", "no") == "yes"
    )
    credential_env = ask("Credential environment variable NAME only; never paste a key") or None
    converse = ask("Enable optional Converse text services? yes/no", "no") == "yes"
    chosen = replace(layout, workspace=Path(workspace).expanduser(), data=Path(data).expanduser())
    tty.write(
        f"Planned config: {chosen.config}\nData: {chosen.data}\nWorkspace: {chosen.workspace}\n"
    )
    tty.write("Provider connection is not checked; saved configuration does not mean ready.\n")
    if ask("Write configuration and private service tokens? type yes", "no") != "yes":
        raise InstallError("Setup cancelled; no configuration written")
    result = setup(
        chosen,
        endpoint=endpoint,
        model=model,
        allow_cloud=allow_cloud,
        credential_env=credential_env,
        converse=converse,
    )
    return _offer_custom_api_profile(chosen, tty, result, store_factory, read_secret)


def _offer_custom_api_profile(layout, tty, result, store_factory, read_secret):
    """Optional #402 step after config is written; never blocks completed setup."""
    from services.distribution import profile_form

    tty.write("Optional: save a named custom OpenAI-compatible API profile now.\n")
    try:
        if not profile_form.confirm(tty, "Add or choose a custom API profile?"):
            return result
        store = (store_factory or profile_form.custom_providers_store)(layout)
        profile_id = profile_form.choose_or_add_profile(
            store, tty, read_secret=read_secret or profile_form.masked_input
        )
    except profile_form.ProfileFormCancelled as exc:
        tty.write(f"{exc} Setup configuration is unchanged.\n")
        return result
    except profile_form.CustomProviderError as exc:
        tty.write(f"Custom API profiles are unavailable: {exc}\n")
        return result
    return {**result, "custom_profile_id": profile_id}
