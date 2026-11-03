"""Owner-driven terminal setup; no credential values, model downloads or inference.

Deterministic ASCII filter adaptation of Kinaiya's moon from the Moonlit Home screen
(`apps/kinaiya_flutter/assets/design/kinaiya-moonlit-desktop-v2.png`: crop=(227, 0, 1279, 458),
disc=(cx=752.5, cy=-74.8, r=530.8), box-downsampled to 68x14, sky floor=0.14, ramp=' .:-=+*#%@').
Original text adaptation under the repository Apache-2.0 license; no runtime image dependency.
"""

from pathlib import Path

from services.distribution.installation import InstallError, check_outdated_system_blockers, setup
from services.distribution.terminal import open_terminal
from services.distribution.wizard_flow import (
    WizardFlowCoordinator,
    run_first_run_wizard,
)
from services.distribution.wizard_flow import (
    run_quick_wizard as _run_quick_wizard,
)

ASCII_MOON_RAMP = " .:-=+*#%@"

MASCOT = "\n".join(
    [
        "%@@@@@ .      :  %+=#:*#++++#@%@@%@@@@@@@@@@@@@@@@+@@%=    =    *@%%",
        ":@%#@@#=.      #*@*=%*.      .*@@@@@@@@@@@@@@@@@@@@--.  =:+=   =@@@-",
        " #@@@%@+%-+=   *= #@@          -@@@@@@@@@@@@@@@@@@@+#  *#*@+ .#@#+%",
        "  @@@+###@@@*=+***%##           =@-#@@@@@@@@@@@@%@%+#%%@@@@+#*@@#@.",
        "  .@@%=@@@@@@@@@*-.=*#          -+-%%@@%@=@@@@@@%=  :%@@@@#@%*#+@:",
        "   .@#%=.+#@@@@@%*  -=%=       :#+:%@%%=::-*+%@@%. :+#@@@@@@%+-%:",
        "     #%%: .*@*@@@@==. ..-=+==+-=:.+@@%      #:%@@@#@@#%##%#@++%",
        "      =%@*. *%#@@@#=#= .  :  -    =%*@-   .+%@@@@@@*#-..*%%*@+",
        "        *@@%*@@#@@@@@%%%:.    :::. #@@@%%*%+-%@@@@%.  :*@@%*",
        "          *@@%%+*%#@@@*..:::    :#@#+**##@@#@%%@@@::-##%@*.",
        "            -%@@+:=#@@@%*==%#=:-+%@+.:-%@%*=:=%@@%%#%@%=",
        "               -#@@@#%@@@@@@@#=--*%+%@@@@#@%@@#@@@@#=",
        "                  .=*%@@@@@@@@@@@@@@@@@@@@@@@@@*=.",
        "                        :-+*#%%@@@@@@%%#*+=:.",
        "                        ( ~~~ Kinaiya ~~~ )",
    ]
)


def render_moon_ascii_from_image(
    image_path: str | Path | None = None,
    *,
    cols: int = 68,
    rows: int = 14,
) -> str:
    """Deterministically apply a box-downsampled luminance ASCII filter to Kinaiya's Home moon."""
    from PIL import Image

    if image_path is None:
        repo_root = Path(__file__).resolve().parents[2]
        image_path = repo_root / "apps/kinaiya_flutter/assets/design/kinaiya-moonlit-desktop-v2.png"

    img = Image.open(image_path).convert("L")
    w, h = img.size
    px = img.load()
    cx, cy, radius = 752.5, -74.80, 530.80
    r_limit_sq = (radius + 3.0) ** 2
    masked = Image.new("L", (w, h), 0)
    m_px = masked.load()
    for y in range(min(h, 460)):
        dy_sq = (y - cy) ** 2
        for x in range(227, min(w, 1279)):
            if (x - cx) ** 2 + dy_sq <= r_limit_sq:
                m_px[x, y] = px[x, y]
    crop = masked.crop((227, 0, 1279, 458))
    small = crop.resize((cols, rows), Image.Resampling.BOX)
    s_px = small.load()
    lines = []
    for r in range(rows):
        row_chars = []
        for c in range(cols):
            val = s_px[c, r] / 255.0
            norm = max(0.0, min(1.0, (val - 0.14) / (1.0 - 0.14)))
            idx = min(len(ASCII_MOON_RAMP) - 1, int(norm * len(ASCII_MOON_RAMP)))
            row_chars.append(ASCII_MOON_RAMP[idx])
        lines.append("".join(row_chars).rstrip())
    lines.append("( ~~~ Kinaiya ~~~ )".center(cols).rstrip())
    return "\n".join(lines)


def mascot(*, width=80, interactive=True):
    if not interactive:
        return "Kinaiya"
    if width < max(map(len, MASCOT.splitlines())):
        return "Kinaiya"[: max(0, width)]
    return MASCOT


def run_quick_wizard(layout, *, tty=None):
    """Run the minimal first-run setup path with safe workspace/data defaults."""
    return _run_quick_wizard(layout, tty=tty, mascot_fn=mascot, setup_fn=setup)


def run_wizard(layout, *, tty=None, mode: str | None = None, profile_id="p01", getpass_fn=None):
    """Use the controlling terminal, so piping the bootstrap cannot consume consent."""
    check_outdated_system_blockers(probe_subprocesses=False)
    if tty is None:
        try:
            with open_terminal() as terminal:
                return run_wizard(
                    layout,
                    tty=terminal,
                    mode=mode,
                    profile_id=profile_id,
                    getpass_fn=getpass_fn,
                )
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
        profile_id=profile_id,
        getpass_fn=getpass_fn,
    )
    return coordinator.run()


__all__ = [
    "ASCII_MOON_RAMP",
    "MASCOT",
    "WizardFlowCoordinator",
    "mascot",
    "render_moon_ascii_from_image",
    "run_first_run_wizard",
    "run_quick_wizard",
    "run_wizard",
]
