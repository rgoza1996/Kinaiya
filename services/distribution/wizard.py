"""Owner-driven terminal setup; no credential values, model downloads or inference.

ASCII adaptation of assets/mascot/kinaiya-mascot.png: swept hair, crescent/star,
robe and ripples. Original text adaptation under the repository Apache-2.0 license.
No runtime image dependency. The artwork's separate provenance is not established.
"""

from services.distribution.installation import (
    InstallError,
    check_outdated_system_blockers,
    reconfigure_section,
    setup,
)
from services.distribution.terminal import open_terminal
from services.distribution.wizard_navigation import WizardNavigator, validate_setup_section

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


def run_wizard(layout, *, tty=None, section: str | None = None):
    """Use the controlling terminal, so piping the bootstrap cannot consume consent."""
    check_outdated_system_blockers(probe_subprocesses=False)
    normalized_section = validate_setup_section(section)
    if tty is None:
        try:
            with open_terminal() as terminal:
                return run_wizard(layout, tty=terminal, section=normalized_section)
        except OSError:
            raise InstallError(
                "No controlling terminal; use setup --yes with explicit endpoint/model"
            ) from None

    navigator = WizardNavigator(
        layout,
        tty=tty,
        section=normalized_section,
        mascot_fn=mascot,
        setup_fn=setup,
        reconfigure_fn=reconfigure_section,
    )
    return navigator.run()


def run_section_wizard(layout, section: str, *, tty=None):
    """Run interactive reconfiguration for one section: workspace, model, or converse."""
    return run_wizard(layout, tty=tty, section=section)
