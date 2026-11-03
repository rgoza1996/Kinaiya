"""Owner-driven terminal setup; no credential values, model downloads or inference.

ASCII adaptation of assets/mascot/kinaiya-mascot.png: swept hair, crescent/star,
robe and ripples. Original text adaptation under the repository Apache-2.0 license.
No runtime image dependency. The artwork's separate provenance is not established.
"""

from services.distribution.installation import InstallError, check_outdated_system_blockers, setup
from services.distribution.terminal import open_terminal
from services.distribution.wizard_flow import (
    WizardFlowCoordinator,
    run_first_run_wizard,
)
from services.distribution.wizard_flow import (
    run_quick_wizard as _run_quick_wizard,
)

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


def run_quick_wizard(layout, *, tty=None):
    """Run the minimal first-run setup path with safe workspace/data defaults."""
    return _run_quick_wizard(layout, tty=tty, mascot_fn=mascot, setup_fn=setup)


def run_wizard(layout, *, tty=None, mode: str | None = None):
    """Use the controlling terminal, so piping the bootstrap cannot consume consent."""
    check_outdated_system_blockers(probe_subprocesses=False)
    if tty is None:
        try:
            with open_terminal() as terminal:
                return run_wizard(layout, tty=terminal, mode=mode)
        except OSError:
            raise InstallError(
                "No controlling terminal; use setup --yes with explicit endpoint/model"
            ) from None

    coordinator = WizardFlowCoordinator(
        layout,
        tty=tty,
        mode=mode,
        mascot_fn=mascot,
        setup_fn=setup,
    )
    return coordinator.run()


__all__ = [
    "MASCOT",
    "WizardFlowCoordinator",
    "mascot",
    "run_first_run_wizard",
    "run_quick_wizard",
    "run_wizard",
]
