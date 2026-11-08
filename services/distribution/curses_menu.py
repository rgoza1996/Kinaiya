"""Interactive curses-based TUI menu engine with arrow-key navigation and fallbacks (#501).

Implements Issue #501:
- curses radiolist and checklist selection primitives.
- Arrow keys (↑/↓), vim keys (k/j), ENTER/SPACE selection, ESC/q cancel, ←/b go-back.
- 60ms ESC disambiguation to prevent premature ESC on slow SSH/tmux networks.
- Stdin buffer flush (termios.tcflush) after curses exit to prevent input bleed.
- In-menu live '/' fuzzy search with ESC query clearing.
- Bracketed-paste sanitization stripping terminal markers.
- Non-TTY, dumb terminal, and KINAIYA_NO_TUI deterministic numbered fallbacks.
- BaseException-derived MenuCancelled and MenuGoBack to avoid being swallowed.
"""

from __future__ import annotations

import curses
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from services.distribution.installation import InstallError
from services.distribution.terminal_ui import (
    ASCII_SYMBOLS,
    PALETTE,
    UNICODE_SYMBOLS,
    ColorMode,
    detect_color_mode,
    supports_unicode,
)

PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"
_PASTE_MARKER_RE = re.compile(r"\x1b\[20[01]~")


class MenuCancelled(BaseException):
    """Raised when the user cancels menu selection via ESC, 'q', or Ctrl-C.

    Inherits from BaseException so broad 'except Exception:' blocks in callers
    cannot accidentally swallow cancellation.
    """


class MenuGoBack(BaseException):
    """Raised when the user requests step rewind via Left Arrow, 'h', or 'b'.

    Inherits from BaseException so broad 'except Exception:' blocks cannot swallow it.
    """


@dataclass(frozen=True)
class MenuItem:
    """A selectable option in a TUI radiolist or checklist."""

    label: str
    value: Any = None
    description: str | None = None
    disabled: bool = False
    recommended: bool = False

    @property
    def effective_value(self) -> Any:
        return self.label if self.value is None else self.value


def sanitize_pasted_input(text: str) -> str:
    """Strip bracketed paste terminal markers and bounding control sequences."""
    if not text:
        return ""
    cleaned = _PASTE_MARKER_RE.sub("", text)
    return cleaned.strip("\r\n")


def flush_stdin() -> None:
    """Drain any pending unread keystrokes from the terminal input buffer.

    curses.endwin() restores terminal modes but leaves buffered keystrokes in the
    OS queue. Calling tcflush prevents keystrokes typed during or immediately after
    curses exit from bleeding into subsequent readline() prompts.
    """
    try:
        import termios

        if hasattr(sys.stdin, "fileno"):
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def fuzzy_score(query: str, text: str) -> float | None:
    """Case-insensitive subsequence matching with word-boundary and prefix bonuses.

    Returns a positive float score if query matches text in order, or None if no match.
    Higher scores indicate better matches.
    """
    if not query:
        return 1.0
    q = query.lower()
    t = text.lower()
    if q == t:
        return 100.0
    if t.startswith(q):
        return 50.0 + (len(q) / len(t)) * 10.0
    if q in t:
        return 30.0 + (len(q) / len(t)) * 5.0

    # Subsequence matching
    score = 0.0
    q_idx = 0
    prev_t_idx = -2
    for t_idx, ch in enumerate(t):
        if q_idx < len(q) and ch == q[q_idx]:
            # Bonus for consecutive matches
            if t_idx == prev_t_idx + 1:
                score += 5.0
            else:
                score += 1.0
            # Bonus for start of word
            if t_idx == 0 or not t[t_idx - 1].isalnum():
                score += 3.0
            prev_t_idx = t_idx
            q_idx += 1
            if q_idx == len(q):
                break

    if q_idx == len(q):
        # Penalty for trailing distance
        score -= (len(t) - len(q)) * 0.1
        return max(score, 0.1)
    return None


def is_curses_available(tty: Any = None, env: dict[str, str] | None = None) -> bool:
    """Check if the environment and stream support full-screen curses TUI."""
    active_env = os.environ if env is None else env
    if active_env.get("KINAIYA_NO_TUI", "").strip().lower() in {"1", "true", "yes"}:
        return False
    term = active_env.get("TERM", "").strip().lower()
    if not term or term in {"dumb", "unknown", "none"}:
        return False
    stream = sys.stdin if tty is None else tty
    if not hasattr(stream, "isatty"):
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _to_menu_items(options: Sequence[str | MenuItem]) -> list[MenuItem]:
    items: list[MenuItem] = []
    for opt in options:
        if isinstance(opt, MenuItem):
            items.append(opt)
        else:
            items.append(MenuItem(label=str(opt)))
    return items


def _curses_radiolist_screen(
    stdscr: Any,
    title: str,
    items: list[MenuItem],
    *,
    default_index: int = 0,
    subtitle: str | None = None,
    allow_back: bool = False,
) -> int:
    """Internal curses loop for single-choice radiolist."""
    try:
        curses.curs_set(0)
    except Exception:
        pass
    try:
        stdscr.keypad(True)
    except Exception:
        pass

    has_colors = False
    try:
        has_colors = curses.has_colors()
        if has_colors:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)     # Accent / highlight
            curses.init_pair(2, curses.COLOR_WHITE, -1)    # Normal text
            curses.init_pair(3, curses.COLOR_GREEN, -1)    # Recommended / success
            curses.init_pair(4, curses.COLOR_YELLOW, -1)   # Warning / Search
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)  # Selected line
    except Exception:
        has_colors = False

    current_idx = max(0, min(default_index, len(items) - 1))
    search_query = ""
    search_active = False

    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        # Filter items if search query is present
        if search_query:
            scored: list[tuple[float, int, MenuItem]] = []
            for orig_idx, item in enumerate(items):
                sc = fuzzy_score(search_query, item.label)
                if sc is not None:
                    scored.append((sc, orig_idx, item))
            scored.sort(key=lambda x: x[0], reverse=True)
            visible_items = [(orig_idx, item) for _, orig_idx, item in scored]
        else:
            visible_items = list(enumerate(items))

        # Clamp current_idx within visible items
        if not visible_items:
            current_vis_idx = 0
        else:
            # Find position of current_idx in visible
            current_vis_idx = 0
            for v_i, (orig_idx, _) in enumerate(visible_items):
                if orig_idx == current_idx:
                    current_vis_idx = v_i
                    break

        # Render Header
        row = 1
        if row < max_y:
            title_text = f" {title} "[: max_x - 2]
            stdscr.addstr(row, 2, title_text, curses.A_BOLD | (curses.color_pair(1) if has_colors else 0))
            row += 1

        if subtitle and row < max_y:
            sub_text = f" {subtitle}"[: max_x - 4]
            stdscr.addstr(row, 2, sub_text, curses.A_DIM)
            row += 1

        row += 1  # Spacing

        # Render Search Bar if active or search query present
        if search_active or search_query:
            if row < max_y:
                search_display = f" Search: {search_query}_"[: max_x - 4]
                stdscr.addstr(
                    row, 2, search_display,
                    curses.A_BOLD | (curses.color_pair(4) if has_colors else 0),
                )
                row += 2

        # Render Options
        start_row = row
        max_options_display = max(1, max_y - start_row - 3)
        scroll_offset = max(0, current_vis_idx - max_options_display + 1)

        for v_i in range(scroll_offset, min(len(visible_items), scroll_offset + max_options_display)):
            orig_idx, item = visible_items[v_i]
            is_selected = (v_i == current_vis_idx)
            is_disabled = item.disabled

            pointer = "›" if is_selected else " "
            radio = "(•)" if is_selected else "( )"
            rec_tag = " (recommended)" if item.recommended else ""
            desc = f" — {item.description}" if item.description else ""

            line = f" {pointer} {radio} {item.label}{rec_tag}{desc}"
            line = line[: max_x - 3]

            attr = 0
            if is_selected:
                attr = curses.A_BOLD | (curses.color_pair(5) if has_colors else curses.A_REVERSE)
            elif is_disabled:
                attr = curses.A_DIM
            elif item.recommended:
                attr = (curses.color_pair(3) if has_colors else curses.A_BOLD)
            else:
                attr = (curses.color_pair(2) if has_colors else 0)

            if row < max_y - 2:
                stdscr.addstr(row, 2, line, attr)
                row += 1

        if not visible_items and row < max_y - 2:
            stdscr.addstr(row, 4, "No matching options found", curses.A_DIM)
            row += 1

        # Render Footer / Help
        footer_row = max_y - 2
        if footer_row > row:
            back_hint = "  ←/b back" if allow_back else ""
            search_hint = "  / search" if not search_active else "  ESC clear search"
            help_text = f" ↑↓/jk navigate  ENTER/SPACE select{back_hint}{search_hint}  q/ESC cancel"
            help_text = help_text[: max_x - 2]
            stdscr.addstr(footer_row, 2, help_text, curses.A_DIM)

        stdscr.refresh()

        # Handle Input
        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            raise MenuCancelled("Menu selection cancelled by user") from None

        if ch == -1:
            continue

        # Handle Search Mode typing
        if search_active:
            if ch in (10, 13):  # Enter: exit search mode, keep selection
                search_active = False
                continue
            if ch == 27:  # ESC: clear search and exit search mode
                search_query = ""
                search_active = False
                continue
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                search_query = search_query[:-1]
                continue
            if 32 <= ch <= 126:
                search_query += chr(ch)
                continue

        # Standard Navigation Mode
        # Arrow Up / k
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            if visible_items:
                current_vis_idx = max(0, current_vis_idx - 1)
                current_idx = visible_items[current_vis_idx][0]
        # Arrow Down / j
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if visible_items:
                current_vis_idx = min(len(visible_items) - 1, current_vis_idx + 1)
                current_idx = visible_items[current_vis_idx][0]
        # Enter or Space
        elif ch in (10, 13, ord(" "), curses.KEY_RIGHT, ord("l"), ord("L")):
            if visible_items:
                selected_item = visible_items[current_vis_idx][1]
                if not selected_item.disabled:
                    return visible_items[current_vis_idx][0]
        # Left arrow / back
        elif allow_back and ch in (curses.KEY_LEFT, ord("h"), ord("H"), ord("b"), ord("B")):
            raise MenuGoBack("User requested step rewind")
        # Search activation
        elif ch == ord("/"):
            search_active = True
        # Quit / Cancel
        elif ch in (ord("q"), ord("Q")):
            raise MenuCancelled("Menu selection cancelled by user")
        # ESC disambiguation
        elif ch == 27:
            # Set 60ms timeout to detect following escape sequence vs bare ESC
            stdscr.timeout(60)
            try:
                next_ch = stdscr.getch()
            finally:
                stdscr.timeout(-1)

            if next_ch == -1:
                # Bare ESC: cancel menu
                if allow_back:
                    raise MenuGoBack("User requested step rewind via ESC")
                raise MenuCancelled("Menu selection cancelled by user via ESC")
            elif next_ch == ord("["):
                # ANSI sequence
                stdscr.timeout(30)
                try:
                    seq = stdscr.getch()
                finally:
                    stdscr.timeout(-1)
                if seq == ord("A"):  # Up
                    if visible_items:
                        current_vis_idx = max(0, current_vis_idx - 1)
                        current_idx = visible_items[current_vis_idx][0]
                elif seq == ord("B"):  # Down
                    if visible_items:
                        current_vis_idx = min(len(visible_items) - 1, current_vis_idx + 1)
                        current_idx = visible_items[current_vis_idx][0]
                elif seq == ord("D") and allow_back:  # Left
                    raise MenuGoBack("User requested step rewind")


def _curses_checklist_screen(
    stdscr: Any,
    title: str,
    items: list[MenuItem],
    *,
    default_indices: Sequence[int] | None = None,
    subtitle: str | None = None,
    allow_back: bool = False,
) -> list[int]:
    """Internal curses loop for multi-choice checklist."""
    try:
        curses.curs_set(0)
    except Exception:
        pass
    try:
        stdscr.keypad(True)
    except Exception:
        pass

    has_colors = False
    try:
        has_colors = curses.has_colors()
        if has_colors:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_WHITE, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
            curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
    except Exception:
        has_colors = False

    selected_set: set[int] = set(default_indices or [])
    current_idx = 0
    search_query = ""
    search_active = False

    while True:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()

        if search_query:
            scored: list[tuple[float, int, MenuItem]] = []
            for orig_idx, item in enumerate(items):
                sc = fuzzy_score(search_query, item.label)
                if sc is not None:
                    scored.append((sc, orig_idx, item))
            scored.sort(key=lambda x: x[0], reverse=True)
            visible_items = [(orig_idx, item) for _, orig_idx, item in scored]
        else:
            visible_items = list(enumerate(items))

        if not visible_items:
            current_vis_idx = 0
        else:
            current_vis_idx = 0
            for v_i, (orig_idx, _) in enumerate(visible_items):
                if orig_idx == current_idx:
                    current_vis_idx = v_i
                    break

        row = 1
        if row < max_y:
            title_text = f" {title} "[: max_x - 2]
            stdscr.addstr(row, 2, title_text, curses.A_BOLD | (curses.color_pair(1) if has_colors else 0))
            row += 1

        if subtitle and row < max_y:
            sub_text = f" {subtitle}"[: max_x - 4]
            stdscr.addstr(row, 2, sub_text, curses.A_DIM)
            row += 1

        row += 1

        if search_active or search_query:
            if row < max_y:
                search_display = f" Search: {search_query}_"[: max_x - 4]
                stdscr.addstr(
                    row, 2, search_display,
                    curses.A_BOLD | (curses.color_pair(4) if has_colors else 0),
                )
                row += 2

        start_row = row
        max_options_display = max(1, max_y - start_row - 3)
        scroll_offset = max(0, current_vis_idx - max_options_display + 1)

        for v_i in range(scroll_offset, min(len(visible_items), scroll_offset + max_options_display)):
            orig_idx, item = visible_items[v_i]
            is_cursor = (v_i == current_vis_idx)
            is_checked = orig_idx in selected_set
            is_disabled = item.disabled

            pointer = "›" if is_cursor else " "
            checkbox = "[✓]" if is_checked else "[ ]"
            rec_tag = " (recommended)" if item.recommended else ""
            desc = f" — {item.description}" if item.description else ""

            line = f" {pointer} {checkbox} {item.label}{rec_tag}{desc}"
            line = line[: max_x - 3]

            attr = 0
            if is_cursor:
                attr = curses.A_BOLD | (curses.color_pair(5) if has_colors else curses.A_REVERSE)
            elif is_disabled:
                attr = curses.A_DIM
            elif is_checked or item.recommended:
                attr = (curses.color_pair(3) if has_colors else curses.A_BOLD)
            else:
                attr = (curses.color_pair(2) if has_colors else 0)

            if row < max_y - 2:
                stdscr.addstr(row, 2, line, attr)
                row += 1

        if not visible_items and row < max_y - 2:
            stdscr.addstr(row, 4, "No matching options found", curses.A_DIM)
            row += 1

        footer_row = max_y - 2
        if footer_row > row:
            back_hint = "  ←/b back" if allow_back else ""
            search_hint = "  / search" if not search_active else "  ESC clear search"
            help_text = f" ↑↓/jk navigate  SPACE toggle  ENTER confirm{back_hint}{search_hint}  q/ESC cancel"
            help_text = help_text[: max_x - 2]
            stdscr.addstr(footer_row, 2, help_text, curses.A_DIM)

        stdscr.refresh()

        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            raise MenuCancelled("Menu selection cancelled by user") from None

        if ch == -1:
            continue

        if search_active:
            if ch in (10, 13):
                search_active = False
                continue
            if ch == 27:
                search_query = ""
                search_active = False
                continue
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                search_query = search_query[:-1]
                continue
            if 32 <= ch <= 126:
                search_query += chr(ch)
                continue

        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            if visible_items:
                current_vis_idx = max(0, current_vis_idx - 1)
                current_idx = visible_items[current_vis_idx][0]
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if visible_items:
                current_vis_idx = min(len(visible_items) - 1, current_vis_idx + 1)
                current_idx = visible_items[current_vis_idx][0]
        elif ch == ord(" "):  # Space toggles
            if visible_items:
                orig_idx, item = visible_items[current_vis_idx]
                if not item.disabled:
                    if orig_idx in selected_set:
                        selected_set.remove(orig_idx)
                    else:
                        selected_set.add(orig_idx)
        elif ch in (10, 13):  # Enter confirms
            return sorted(selected_set)
        elif allow_back and ch in (curses.KEY_LEFT, ord("h"), ord("H"), ord("b"), ord("B")):
            raise MenuGoBack("User requested step rewind")
        elif ch == ord("/"):
            search_active = True
        elif ch in (ord("q"), ord("Q")):
            raise MenuCancelled("Menu selection cancelled by user")
        elif ch == 27:
            stdscr.timeout(60)
            try:
                next_ch = stdscr.getch()
            finally:
                stdscr.timeout(-1)

            if next_ch == -1:
                if allow_back:
                    raise MenuGoBack("User requested step rewind via ESC")
                raise MenuCancelled("Menu selection cancelled by user via ESC")
            elif next_ch == ord("["):
                stdscr.timeout(30)
                try:
                    seq = stdscr.getch()
                finally:
                    stdscr.timeout(-1)
                if seq == ord("A"):
                    if visible_items:
                        current_vis_idx = max(0, current_vis_idx - 1)
                        current_idx = visible_items[current_vis_idx][0]
                elif seq == ord("B"):
                    if visible_items:
                        current_vis_idx = min(len(visible_items) - 1, current_vis_idx + 1)
                        current_idx = visible_items[current_vis_idx][0]
                elif seq == ord("D") and allow_back:
                    raise MenuGoBack("User requested step rewind")


def _line_radiolist_fallback(
    tty: Any,
    title: str,
    items: list[MenuItem],
    *,
    default_index: int = 0,
    subtitle: str | None = None,
    allow_back: bool = False,
) -> int:
    """Deterministic numbered line prompt fallback for non-TTY / dumb terminals."""
    color_mode = detect_color_mode(tty)
    unicode_ok = supports_unicode(tty)
    symbols = UNICODE_SYMBOLS if unicode_ok else ASCII_SYMBOLS

    out = [f"\n=== {title} ==="]
    if subtitle:
        out.append(f"  {subtitle}")

    default_choice = default_index + 1 if 0 <= default_index < len(items) else 1
    for i, item in enumerate(items, 1):
        rec_tag = " (recommended)" if item.recommended else ""
        desc = f" — {item.description}" if item.description else ""
        out.append(f"  [{i}] {item.label}{rec_tag}{desc}")

    back_hint = " or :back" if allow_back else ""
    prompt = f"\nSelect option [1-{len(items)}] (default {default_choice}){back_hint}: "

    tty.write("\n".join(out) + "\n" + prompt)
    tty.flush()

    try:
        raw = tty.readline()
    except (KeyboardInterrupt, EOFError):
        tty.write("\n")
        tty.flush()
        raise MenuCancelled("Menu selection cancelled") from None

    if not raw:
        raise MenuCancelled("Terminal input closed")

    choice = raw.strip()
    if allow_back and choice.lower() in (":back", "b", "back"):
        raise MenuGoBack("User requested step rewind")
    if choice.lower() in (":cancel", "q", "quit", ":q"):
        raise MenuCancelled("Menu selection aborted")

    if not choice:
        return default_choice - 1

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(items):
            return idx - 1

    # Exact or prefix match on label
    for i, item in enumerate(items):
        if item.label.lower() == choice.lower():
            return i

    raise InstallError(f"Invalid option selection {choice!r}")


def _line_checklist_fallback(
    tty: Any,
    title: str,
    items: list[MenuItem],
    *,
    default_indices: Sequence[int] | None = None,
    subtitle: str | None = None,
    allow_back: bool = False,
) -> list[int]:
    """Deterministic line prompt fallback for checklist."""
    out = [f"\n=== {title} ==="]
    if subtitle:
        out.append(f"  {subtitle}")

    default_set = set(default_indices or [])
    for i, item in enumerate(items, 1):
        checked = "[X]" if (i - 1) in default_set else "[ ]"
        rec_tag = " (recommended)" if item.recommended else ""
        desc = f" — {item.description}" if item.description else ""
        out.append(f"  {checked} [{i}] {item.label}{rec_tag}{desc}")

    back_hint = " or :back" if allow_back else ""
    prompt = f"\nSelect options (comma-separated, e.g. 1,2, or ENTER for defaults){back_hint}: "

    tty.write("\n".join(out) + "\n" + prompt)
    tty.flush()

    try:
        raw = tty.readline()
    except (KeyboardInterrupt, EOFError):
        tty.write("\n")
        tty.flush()
        raise MenuCancelled("Menu selection cancelled") from None

    if not raw:
        raise MenuCancelled("Terminal input closed")

    choice = raw.strip()
    if allow_back and choice.lower() in (":back", "b", "back"):
        raise MenuGoBack("User requested step rewind")
    if choice.lower() in (":cancel", "q", "quit", ":q"):
        raise MenuCancelled("Menu selection aborted")

    if not choice:
        return sorted(default_set)

    selected: set[int] = set()
    for part in choice.split(","):
        p = part.strip()
        if p.isdigit():
            idx = int(p)
            if 1 <= idx <= len(items):
                selected.add(idx - 1)
        else:
            for i, item in enumerate(items):
                if item.label.lower() == p.lower():
                    selected.add(i)

    return sorted(selected)


def curses_radiolist(
    title: str,
    options: Sequence[str | MenuItem],
    *,
    default_index: int = 0,
    subtitle: str | None = None,
    allow_back: bool = False,
    tty: Any = None,
) -> int:
    """Interactively select a single option using arrow keys with fallbacks.

    Returns the integer index of the selected item in options.
    Raises MenuCancelled if the user cancels, or MenuGoBack if the user requests rewind.
    """
    items = _to_menu_items(options)
    if not items:
        raise InstallError("No options available to select")

    active_tty = sys.stdin if tty is None else tty
    if not is_curses_available(active_tty):
        return _line_radiolist_fallback(
            active_tty,
            title,
            items,
            default_index=default_index,
            subtitle=subtitle,
            allow_back=allow_back,
        )

    try:
        return curses.wrapper(
            _curses_radiolist_screen,
            title,
            items,
            default_index=default_index,
            subtitle=subtitle,
            allow_back=allow_back,
        )
    finally:
        flush_stdin()


def curses_checklist(
    title: str,
    options: Sequence[str | MenuItem],
    *,
    default_indices: Sequence[int] | None = None,
    subtitle: str | None = None,
    allow_back: bool = False,
    tty: Any = None,
) -> list[int]:
    """Interactively select zero or more options with checkboxes.

    Returns the list of integer indices of checked items.
    Raises MenuCancelled if the user cancels, or MenuGoBack if the user requests rewind.
    """
    items = _to_menu_items(options)
    if not items:
        raise InstallError("No options available to select")

    active_tty = sys.stdin if tty is None else tty
    if not is_curses_available(active_tty):
        return _line_checklist_fallback(
            active_tty,
            title,
            items,
            default_indices=default_indices,
            subtitle=subtitle,
            allow_back=allow_back,
        )

    try:
        return curses.wrapper(
            _curses_checklist_screen,
            title,
            items,
            default_indices=default_indices,
            subtitle=subtitle,
            allow_back=allow_back,
        )
    finally:
        flush_stdin()
