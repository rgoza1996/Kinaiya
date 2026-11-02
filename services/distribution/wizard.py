"""Owner-driven terminal setup; no credential values, model downloads or inference.

ASCII adaptation of assets/mascot/kinaiya-mascot.png: swept hair, crescent/star,
robe and ripples. Original text adaptation under the repository Apache-2.0 license.
No runtime image dependency. The artwork's separate provenance is not established.
"""

import shutil
from dataclasses import replace
from pathlib import Path

from services.distribution.installation import InstallError, setup
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


def run_wizard(layout, *, tty=None):
    """Use the controlling terminal, so piping the bootstrap cannot consume consent."""
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
    return setup(
        chosen,
        endpoint=endpoint,
        model=model,
        allow_cloud=allow_cloud,
        credential_env=credential_env,
        converse=converse,
    )
