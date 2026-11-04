"""Owner-driven terminal setup; no credential values, model downloads or inference.

Deterministic ASCII filter adaptation of Kinaiya's moon from the Moonlit Home screen
(`apps/kinaiya_flutter/assets/design/kinaiya-moonlit-desktop-v2.png`: crop=(227, 0, 1279, 458),
disc=(cx=752.5, cy=-74.8, r=530.8), box-downsampled to 68x14, sky floor=0.14, ramp=' .:-=+*#%@').
Original text adaptation under the repository Apache-2.0 license; no runtime image dependency.
"""

import os
import struct
import zlib
from pathlib import Path
from typing import Any

from services.distribution.installation import InstallError, check_outdated_system_blockers, setup
from services.distribution.terminal import open_terminal
from services.distribution.terminal_ui import NON_COLOR_TERMS, ColorMode, detect_color_mode
from services.distribution.wizard_flow import (
    WizardFlowCoordinator,
    run_first_run_wizard,
)
from services.distribution.wizard_flow import (
    run_quick_wizard as _run_quick_wizard,
)

ASCII_MOON_RAMP = " .:-=+*#%@"

ANSI_RESET = "\x1b[0m"
ANSI_MOON_BRIGHT = "\x1b[38;2;248;250;252m"
ANSI_MOON_HIGH = "\x1b[38;2;147;197;253m"
ANSI_MOON_MID = "\x1b[38;2;56;189;248m"
ANSI_MOON_RIM = "\x1b[38;2;129;140;248m"
ANSI_MOON_GOLD = "\x1b[38;2;251;191;36m"
ANSI_MOON_CYAN = "\x1b[38;2;56;189;248m"

ANSI16_MOON_BRIGHT = "\x1b[97m"
ANSI16_MOON_HIGH = "\x1b[96m"
ANSI16_MOON_MID = "\x1b[36m"
ANSI16_MOON_RIM = "\x1b[94m"
ANSI16_MOON_GOLD = "\x1b[33m"
ANSI16_MOON_CYAN = "\x1b[96m"

MASCOT = "\n".join(
    [
        "%@@@@@ .      :  %++#:*#++++#@%@@%@@@@@@@@@@@@@@@@+@@%=    =    +@%%",
        "-@%*@@#=.      #*@*=%*.      .*@@@@@@@@@@@@@@@@@@@@--.  =:+=   =@@%=",
        " #@@@%@+%-+=   *= #@@          -@@@@@@@@@@@@@@@@@@@+#  *#*@+ .#@#+%",
        "  @@@+###@@@*=+***%##           =@-#@@@@@@@@@@@@%@%+#%%@@@@+#*@@#@.",
        "  .@@%+@@@@@@@@@*-.=*#          -*=%%@@%@=@@@@@@%=  :%@@@@#@%*#+@:",
        "   .@*%=.+#@@@@@%*  -=%=       :#+:%@%%=::-*+%@@%. :+#@@@@@@%+-#:",
        "     #%%: .*@*@@@@==  ..-=+==+-=:.+@@%      #:%@@@#@@*%##%#@++%",
        "      =%@*. +%#@@@#=#= .  :  -    +%*@-   .=%@@@@@@##-..*%%*@+",
        "        *@@%*@@#@@@@@%%#-.    :::. #@@@%%*%+-%@@@@%.  :*@@%*",
        "          *@@%%+*%#@@@*. :::    :#@#+**##@@#@%%@@@::-##%@*",
        "            -%@@+:=#@@@%*==%#+:-+%@+.:-%@%*=:=%@@%%#%@%=",
        "               -#@@@#%@@@@@@@#=--+%+%@@@@#@%@@%@@@@#=",
        "                  .=*%@@@@@@@@@@@@@@@@@@@@@@@@%*=.",
        "                        :-+*#%%@@@@@@%%#*+=:.",
        "                        ( ~~~ Kinaiya ~~~ )",
    ]
)


def _env_blocks_color(env: dict[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    if (
        environ.get("NO_COLOR", "") != ""
        or environ.get("KINAIYA_NO_COLOR", "") != ""
        or environ.get("CLICOLOR", "") == "0"
    ):
        return True
    term = environ.get("TERM", "").strip().lower()
    return bool(term in NON_COLOR_TERMS or term.endswith("-m") or term.endswith("-mono"))


def tty_supports_color(tty: Any = None, *, env: dict[str, str] | None = None) -> bool:
    """Return True when ANSI terminal color is supported and not disabled via NO_COLOR/dumb."""
    if _env_blocks_color(env):
        return False
    environ = os.environ if env is None else env
    if environ.get("KINAIYA_FORCE_COLOR") == "1":
        return True
    return detect_color_mode(tty, env) != ColorMode.NONE


def colorize_mascot(
    text: str = MASCOT,
    *,
    mode: ColorMode | str = ColorMode.TRUECOLOR,
) -> str:
    """Apply Kinaiya's Moonlit ANSI truecolor or 16-color palette to the 68x14 ASCII Moon."""
    resolved = ColorMode(mode) if not isinstance(mode, ColorMode) else mode
    if resolved == ColorMode.NONE:
        return text
    if resolved == ColorMode.ANSI16:
        c_bright = ANSI16_MOON_BRIGHT
        c_high = ANSI16_MOON_HIGH
        c_mid = ANSI16_MOON_MID
        c_rim = ANSI16_MOON_RIM
        c_gold = ANSI16_MOON_GOLD
        c_cyan = ANSI16_MOON_CYAN
    else:
        c_bright = ANSI_MOON_BRIGHT
        c_high = ANSI_MOON_HIGH
        c_mid = ANSI_MOON_MID
        c_rim = ANSI_MOON_RIM
        c_gold = ANSI_MOON_GOLD
        c_cyan = ANSI_MOON_CYAN

    colored_lines: list[str] = []
    raw_lines = text.splitlines()
    for row_idx, line in enumerate(raw_lines):
        if row_idx == len(raw_lines) - 1 and "Kinaiya" in line:
            colored_lines.append(
                line.replace("(", f"{c_rim}({ANSI_RESET}")
                .replace("~~~", f"{c_gold}~~~{ANSI_RESET}")
                .replace("Kinaiya", f"{c_cyan}Kinaiya{ANSI_RESET}")
                .replace(")", f"{c_rim}){ANSI_RESET}")
            )
            continue
        out: list[str] = []
        active_code = ""
        for ch in line:
            if ch == " ":
                if active_code:
                    out.append(ANSI_RESET)
                    active_code = ""
                out.append(" ")
                continue
            if ch in "@#%":
                code = c_bright
            elif ch in "*+":
                code = c_high
            elif ch in "=-":
                code = c_mid
            else:
                code = c_rim
            if code != active_code:
                out.append(code)
                active_code = code
            out.append(ch)
        if active_code:
            out.append(ANSI_RESET)
        colored_lines.append("".join(out))
    return "\n".join(colored_lines)


def render_moon_ascii_from_image(
    image_path: str | Path | None = None,
    *,
    cols: int = 68,
    rows: int = 14,
) -> str:
    """Deterministically apply a pure-stdlib box-downsampled luminance ASCII filter to Kinaiya's Home moon."""
    if image_path is None:
        repo_root = Path(__file__).resolve().parents[2]
        image_path = repo_root / "apps/kinaiya_flutter/assets/design/kinaiya-moonlit-desktop-v2.png"

    raw = Path(image_path).read_bytes()
    pos = 8
    idat = bytearray()
    w = h = color_type = 0
    while pos < len(raw):
        length, ctype = struct.unpack(">I4s", raw[pos : pos + 8])
        chunk = raw[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, _, color_type, _, _, _ = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break

    bpp = 4 if color_type == 6 else (3 if color_type == 2 else 1)
    stride = w * bpp
    decomp = zlib.decompressobj()
    crop_h = min(h, 458)
    buf = decomp.decompress(bytes(idat), max_length=(stride + 1) * crop_h)
    prev = bytearray(stride)
    cur = bytearray(stride)
    cx, cy, radius = 752.5, -74.80, 530.80
    r_limit_sq = (radius + 3.0) ** 2
    x0, x1 = 227, min(w, 1279)
    crop_w = x1 - x0
    lum_rows: list[list[float]] = []

    for y in range(crop_h):
        row_start = y * (stride + 1)
        f_type = buf[row_start]
        scan = buf[row_start + 1 : row_start + 1 + stride]
        if f_type == 0:
            cur[:] = scan
        elif f_type == 1:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                cur[i] = (scan[i] + a) & 0xFF
        elif f_type == 2:
            for i in range(stride):
                cur[i] = (scan[i] + prev[i]) & 0xFF
        elif f_type == 3:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                cur[i] = (scan[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f_type == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (scan[i] + pr) & 0xFF
        prev[:] = cur
        dy_sq = (y - cy) ** 2
        row_lum = [0.0] * crop_w
        for x in range(x0, x1):
            if (x - cx) ** 2 + dy_sq <= r_limit_sq:
                off = x * bpp
                if bpp >= 3:
                    val = (
                        cur[off] * 19595 + cur[off + 1] * 38470 + cur[off + 2] * 7471 + 32768
                    ) >> 16
                else:
                    val = cur[off]
                row_lum[x - x0] = float(val)
        lum_rows.append(row_lum)

    lines: list[str] = []
    for r in range(rows):
        y_start = r * crop_h / rows
        y_end = (r + 1) * crop_h / rows
        yi0 = int(y_start)
        yi1 = min(crop_h, int(y_end) + (1 if y_end > int(y_end) else 0))
        row_chars = []
        for c in range(cols):
            x_start = c * crop_w / cols
            x_end = (c + 1) * crop_w / cols
            xi0 = int(x_start)
            xi1 = min(crop_w, int(x_end) + (1 if x_end > int(x_end) else 0))
            total = 0.0
            w_sum = 0.0
            for yy in range(yi0, yi1):
                wy = min(yy + 1.0, y_end) - max(float(yy), y_start)
                r_lum = lum_rows[yy]
                for xx in range(xi0, xi1):
                    wx = min(xx + 1.0, x_end) - max(float(xx), x_start)
                    w_xy = wy * wx
                    total += r_lum[xx] * w_xy
                    w_sum += w_xy
            avg = round(total / w_sum) / 255.0
            norm = max(0.0, min(1.0, (avg - 0.14) / (1.0 - 0.14)))
            idx = min(len(ASCII_MOON_RAMP) - 1, int(norm * len(ASCII_MOON_RAMP)))
            row_chars.append(ASCII_MOON_RAMP[idx])
        lines.append("".join(row_chars).rstrip())
    lines.append("( ~~~ Kinaiya ~~~ )".center(cols).rstrip())
    return "\n".join(lines)


def mascot(
    *,
    width: int = 80,
    interactive: bool = True,
    color: bool | ColorMode | str = False,
    tty: Any = None,
    env: dict[str, str] | None = None,
) -> str:
    if not interactive:
        return "Kinaiya"
    if width < max(map(len, MASCOT.splitlines())):
        return "Kinaiya"[: max(0, width)]
    if _env_blocks_color(env):
        return MASCOT
    if isinstance(color, (ColorMode, str)) and not isinstance(color, bool):
        resolved = ColorMode(color) if not isinstance(color, ColorMode) else color
        return colorize_mascot(MASCOT, mode=resolved)
    if color is True:
        if tty is not None:
            mode = detect_color_mode(tty, env)
            return colorize_mascot(MASCOT, mode=mode)
        return colorize_mascot(MASCOT, mode=ColorMode.TRUECOLOR)
    if tty is not None:
        return colorize_mascot(MASCOT, mode=detect_color_mode(tty, env))
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
    "colorize_mascot",
    "mascot",
    "render_moon_ascii_from_image",
    "run_first_run_wizard",
    "run_quick_wizard",
    "run_wizard",
    "tty_supports_color",
]
