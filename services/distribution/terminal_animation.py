"""Optional terminal intro animation and branded activity indicator.

Presentation-only helpers for interactive TTY sessions. Respects NO_COLOR,
REDUCED_MOTION, KINAIYA_NO_ANIMATION, CI, dumb terminals, narrow widths, and
non-TTY streams. Never delays completed work or implies service readiness.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

CURSOR_HIDE = "\x1b[?25l"
CURSOR_SHOW = "\x1b[?25h"
CLEAR_LINE = "\x1b[2K\r"

MOON_FRAMES: tuple[str, ...] = ("☾", "◓", "◑", "◒")

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

_SHIMMER_FRAME_1 = "\n".join(
    [
        "%@@@@@ ☾      ·  %++#:*#++++#@%@@%@@@@@@@@@@@@@@@@+@@%=    =    +@%%",
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
        "                        ( ☾~~ Kinaiya ~~☾ )",
    ]
)

_SHIMMER_FRAME_2 = "\n".join(
    [
        "%@@@@@ .      ✧  %++#:*#++++#@%@@%@@@@@@@@@@@@@@@@+@@%=    =    +@%%",
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
        "                        ( ✧~~ Kinaiya ~~✧ )",
    ]
)

INTRO_FRAMES: tuple[str, ...] = (_SHIMMER_FRAME_1, _SHIMMER_FRAME_2, MASCOT)
MIN_ANIMATION_WIDTH = 68


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    return normalized not in {"", "0", "false", "no", "off"}


def _stream_is_tty(stream: Any) -> bool:
    if stream is None:
        return False
    isatty_fn = getattr(stream, "isatty", None)
    if not callable(isatty_fn):
        return False
    try:
        return bool(isatty_fn())
    except (OSError, ValueError):
        return False


def should_animate(
    stream: Any,
    env: Mapping[str, str] | None = None,
    width: int = 80,
) -> bool:
    """Return True only when rich TTY animation is safe and permitted."""
    if not _stream_is_tty(stream):
        return False
    if type(width) is not int or width < MIN_ANIMATION_WIDTH:
        return False
    environ = os.environ if env is None else env
    if "NO_COLOR" in environ and str(environ.get("NO_COLOR", "")).strip() != "0":
        return False
    if _is_truthy(environ.get("REDUCED_MOTION")):
        return False
    if _is_truthy(environ.get("KINAIYA_NO_ANIMATION")):
        return False
    if _is_truthy(environ.get("CI")):
        return False
    term = str(environ.get("TERM", "xterm")).strip().lower()
    if not term or term == "dumb":
        return False
    return True


def static_mascot_frame(*, width: int = 80, interactive: bool = True) -> str:
    """Return a clean static mascot or compact wordmark without escape sequences."""
    if not interactive or type(width) is not int or width < MIN_ANIMATION_WIDTH:
        limit = max(0, width if type(width) is int else 7)
        return "Kinaiya"[:limit]
    max_line = max(len(line) for line in MASCOT.splitlines())
    if width < max_line:
        return "Kinaiya"[: max(0, width)]
    return MASCOT


def render_intro_sequence(
    stream: Any,
    *,
    width: int = 80,
    env: Mapping[str, str] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
    max_duration_s: float = 0.18,
    colorize_fn: Callable[[str], str] | None = None,
) -> str:
    """Render the Kinaiya intro sequence and always leave a clean static final frame."""
    is_tty = _stream_is_tty(stream)
    paint = colorize_fn if colorize_fn is not None else (lambda text: text)
    if not should_animate(stream, env=env, width=width):
        static_text = static_mascot_frame(width=width, interactive=is_tty)
        rendered_static = paint(static_text) if static_text == MASCOT else static_text
        if stream is not None:
            stream.write(rendered_static + "\n")
            if hasattr(stream, "flush"):
                stream.flush()
        return static_text

    budget = max(0.0, min(float(max_duration_s), 0.5))
    line_count = len(MASCOT.splitlines())
    step_delay = budget / max(1, len(INTRO_FRAMES) - 1)
    started = clock_fn()
    drew_frame = False
    completed_final = False
    final_painted = paint(MASCOT)

    try:
        stream.write(CURSOR_HIDE)
        if hasattr(stream, "flush"):
            stream.flush()
        for frame in INTRO_FRAMES[:-1]:
            if clock_fn() - started >= budget:
                break
            if drew_frame:
                stream.write(f"\x1b[{line_count}A\r")
            stream.write(paint(frame) + "\n")
            if hasattr(stream, "flush"):
                stream.flush()
            drew_frame = True
            remaining = budget - (clock_fn() - started)
            if remaining <= 0:
                break
            sleep_fn(min(step_delay, remaining))

        if drew_frame:
            stream.write(f"\x1b[{line_count}A\r")
        stream.write(final_painted + "\n")
        completed_final = True
    finally:
        if not completed_final:
            if drew_frame:
                stream.write(f"\x1b[{line_count}A\r")
            stream.write(final_painted + "\n")
        stream.write(CURSOR_SHOW)
        if hasattr(stream, "flush"):
            stream.flush()

    return MASCOT


class BrandedActivityIndicator:
    """Bounded activity indicator that never implies completion without finish()."""

    def __init__(
        self,
        stream: Any,
        label: str,
        *,
        width: int = 80,
        env: Mapping[str, str] | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stream = stream
        self.label = " ".join(str(label).splitlines()).strip() or "Working"
        self.width = width if type(width) is int and width > 0 else 80
        self.env = env
        self.clock_fn = clock_fn
        self.animated = should_animate(stream, env=env, width=self.width)
        self.active = False
        self.finished = False
        self.status: str | None = None
        self._frame_index = 0
        self._cursor_hidden = False
        self._started_at: float | None = None

    def _format_text(self, detail: str | None = None, *, reserve: int = 8) -> str:
        clean_detail = " ".join(str(detail).splitlines()).strip() if detail else ""
        combined = f"{self.label}: {clean_detail}" if clean_detail else self.label
        max_chars = max(4, self.width - reserve)
        if len(combined) > max_chars:
            return combined[: max_chars - 3] + "..."
        return combined

    def __enter__(self) -> BrandedActivityIndicator:
        self.active = True
        self._started_at = self.clock_fn()
        if self.stream is not None:
            if self.animated:
                text = self._format_text(reserve=4)
                self.stream.write(f"{CURSOR_HIDE}{CLEAR_LINE}{MOON_FRAMES[0]} {text}")
                self._cursor_hidden = True
            else:
                text = self._format_text(reserve=6)
                self.stream.write(f"[...] {text}\n")
            if hasattr(self.stream, "flush"):
                self.stream.flush()
        return self

    def tick(self, detail: str | None = None) -> None:
        """Advance the branded indicator by one step without blocking."""
        if not self.active or self.finished or self.stream is None:
            return
        self._frame_index = (self._frame_index + 1) % len(MOON_FRAMES)
        if self.animated:
            glyph = MOON_FRAMES[self._frame_index]
            text = self._format_text(detail, reserve=4)
            self.stream.write(f"{CLEAR_LINE}{glyph} {text}")
            if hasattr(self.stream, "flush"):
                self.stream.flush()

    step = tick

    def finish(self, status: str = "pass", message: str | None = None) -> None:
        """Record authoritative completion status without waiting for an animation cycle."""
        if status not in {"pass", "fail"}:
            raise ValueError(f"Unsupported activity finish status: {status!r}")
        if self.finished:
            return
        self.finished = True
        self.status = status
        badge = "[PASS]" if status == "pass" else "[FAIL]"
        text = self._format_text(message, reserve=len(badge) + 2)
        if self.stream is not None:
            if self.animated:
                self.stream.write(f"{CLEAR_LINE}{badge} {text}\n{CURSOR_SHOW}")
                self._cursor_hidden = False
            else:
                self.stream.write(f"{badge} {text}\n")
            if hasattr(self.stream, "flush"):
                self.stream.flush()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if self.stream is not None and not self.finished:
                if exc_type is not None:
                    text = self._format_text(reserve=14)
                    if self.animated:
                        self.stream.write(f"{CLEAR_LINE}[INTERRUPTED] {text}\n")
                    else:
                        self.stream.write(f"[INTERRUPTED] {text}\n")
                elif self.animated:
                    self.stream.write(CLEAR_LINE)
        finally:
            self.active = False
            if self.stream is not None:
                if self._cursor_hidden or self.animated:
                    self.stream.write(CURSOR_SHOW)
                    self._cursor_hidden = False
                if hasattr(self.stream, "flush"):
                    self.stream.flush()
        return False
