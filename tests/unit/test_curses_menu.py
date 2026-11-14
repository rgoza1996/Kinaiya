"""Unit tests for curses TUI menu engine, arrow-key navigation, and fallbacks (#501)."""

from __future__ import annotations

import curses
from unittest.mock import MagicMock, patch

import pytest

from services.distribution.curses_menu import (
    MenuCancelled,
    MenuGoBack,
    MenuItem,
    _curses_checklist_screen,
    _curses_radiolist_screen,
    curses_checklist,
    curses_radiolist,
    flush_stdin,
    fuzzy_score,
    is_curses_available,
    sanitize_pasted_input,
)
from services.distribution.installation import InstallError


class _FakeTTY:
    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.output = ""

    def write(self, text: str) -> None:
        self.output += text

    def flush(self) -> None:
        pass

    def readline(self) -> str:
        try:
            return next(self._answers) + "\n"
        except StopIteration:
            return ""

    def isatty(self) -> bool:
        return False


def test_menu_exceptions_inherit_from_base_exception():
    """MenuCancelled and MenuGoBack must not be swallowed by 'except Exception:'."""
    assert issubclass(MenuCancelled, BaseException)
    assert not issubclass(MenuCancelled, Exception)

    assert issubclass(MenuGoBack, BaseException)
    assert not issubclass(MenuGoBack, Exception)

    caught_by_generic = False
    try:
        raise MenuCancelled("test")
    except Exception:
        caught_by_generic = True
    except BaseException:
        pass
    assert not caught_by_generic


def test_sanitize_pasted_input():
    # Empty
    assert sanitize_pasted_input("") == ""
    # Normal string
    assert sanitize_pasted_input("sk-ant-12345") == "sk-ant-12345"
    # Bracketed paste markers
    pasted = "\x1b[200~sk-ant-secret-key-123\x1b[201~"
    assert sanitize_pasted_input(pasted) == "sk-ant-secret-key-123"
    # Newlines
    assert (
        sanitize_pasted_input("\r\n  http://localhost:11434/v1  \n")
        == "  http://localhost:11434/v1  "
    )


def test_flush_stdin_safely_handles_errors():
    """flush_stdin must never raise even if termios throws or stdin has no fileno."""
    flush_stdin()  # Safe execution in test environment


def test_fuzzy_score():
    assert fuzzy_score("", "anything") == 1.0
    assert fuzzy_score("test", "test") == 100.0

    # Prefix match
    score_prefix = fuzzy_score("qwen", "qwen-2.5-coder")
    assert score_prefix is not None
    assert score_prefix > 50.0

    # Substring match
    score_sub = fuzzy_score("coder", "qwen-2.5-coder-32b")
    assert score_sub is not None
    assert score_sub > 30.0

    # Subsequence match
    score_subseq = fuzzy_score("q25c", "qwen-2.5-coder")
    assert score_subseq is not None

    # Non-match
    assert fuzzy_score("llama", "mistral-7b") is None


def test_is_curses_available():
    # Dumb terminal
    assert not is_curses_available(env={"TERM": "dumb"})
    # Explicit NO_TUI
    assert not is_curses_available(env={"TERM": "xterm-256color", "KINAIYA_NO_TUI": "1"})
    # Non-TTY stream
    fake_tty = _FakeTTY([])
    assert not is_curses_available(fake_tty, env={"TERM": "xterm-256color"})


def test_line_radiolist_fallback():
    options = [
        MenuItem(
            label="LM Studio", description="Local loopback http://127.0.0.1:1234", recommended=True
        ),
        MenuItem(label="Meta / Muse Cloud", description="Managed cloud service"),
        MenuItem(label="Custom endpoint", description="Manual URL"),
    ]

    # Select default (option 1) on ENTER
    tty = _FakeTTY([""])
    idx = curses_radiolist("Select Provider", options, default_index=0, tty=tty)
    assert idx == 0
    assert "=== Select Provider ===" in tty.output
    assert "(recommended)" in tty.output

    # Select option 2
    tty = _FakeTTY(["2"])
    idx = curses_radiolist("Select Provider", options, default_index=0, tty=tty)
    assert idx == 1

    # Select by label
    tty = _FakeTTY(["Custom endpoint"])
    idx = curses_radiolist("Select Provider", options, default_index=0, tty=tty)
    assert idx == 2

    # Cancel via 'q'
    tty = _FakeTTY(["q"])
    with pytest.raises(MenuCancelled):
        curses_radiolist("Select Provider", options, tty=tty)

    # Cancel via ':cancel'
    tty = _FakeTTY([":cancel"])
    with pytest.raises(MenuCancelled):
        curses_radiolist("Select Provider", options, tty=tty)

    # Back via ':back' when allow_back=True
    tty = _FakeTTY([":back"])
    with pytest.raises(MenuGoBack):
        curses_radiolist("Select Provider", options, allow_back=True, tty=tty)

    # Invalid input
    tty = _FakeTTY(["invalid-999"])
    with pytest.raises(InstallError, match="Invalid option selection"):
        curses_radiolist("Select Provider", options, tty=tty)


def test_line_checklist_fallback():
    options = [
        MenuItem(label="Agent Service", recommended=True),
        MenuItem(label="Converse Dialogue"),
        MenuItem(label="Decisions Router"),
    ]

    # Default indices (0 and 1) on ENTER
    tty = _FakeTTY([""])
    indices = curses_checklist("Select Services", options, default_indices=[0, 1], tty=tty)
    assert indices == [0, 1]

    # Select 1,3
    tty = _FakeTTY(["1,3"])
    indices = curses_checklist("Select Services", options, tty=tty)
    assert indices == [0, 2]

    # Cancel via 'q'
    tty = _FakeTTY(["q"])
    with pytest.raises(MenuCancelled):
        curses_checklist("Select Services", options, tty=tty)


def test_mock_curses_radiolist_screen():
    items = [
        MenuItem(label="Option A"),
        MenuItem(label="Option B"),
        MenuItem(label="Option C"),
    ]

    # Simulate stdscr with mock getch returning Down arrow then Enter
    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [
        curses.KEY_DOWN,  # Move to Option B
        10,  # ENTER
    ]

    selected = _curses_radiolist_screen(mock_stdscr, "Test Title", items, default_index=0)
    assert selected == 1


def test_mock_curses_radiolist_esc_disambiguation():
    items = [MenuItem(label="Option A"), MenuItem(label="Option B")]

    # 1. Bare ESC -> timeout returns -1 -> MenuCancelled
    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [
        27,  # ESC
        -1,  # timeout
    ]
    with pytest.raises(MenuCancelled):
        _curses_radiolist_screen(mock_stdscr, "Test", items)

    # 2. ESC followed by '[' and 'B' (Down arrow ANSI sequence) then ENTER
    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [
        27,  # ESC
        ord("["),  # CSI
        ord("B"),  # Down arrow
        10,  # ENTER
    ]
    selected = _curses_radiolist_screen(mock_stdscr, "Test", items)
    assert selected == 1


def test_mock_curses_checklist_screen():
    items = [
        MenuItem(label="Option A"),
        MenuItem(label="Option B"),
    ]

    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [
        ord(" "),  # Space toggles Option A
        curses.KEY_DOWN,
        ord(" "),  # Space toggles Option B
        10,  # ENTER confirms
    ]

    result = _curses_checklist_screen(mock_stdscr, "Test Checklist", items, default_indices=[])
    assert result == [0, 1]


def test_mock_curses_search_and_select():
    items = [
        MenuItem(label="Apple"),
        MenuItem(label="Banana"),
        MenuItem(label="Cherry"),
    ]

    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [
        ord("/"),  # enter search mode
        ord("c"),  # query 'c'
        ord("h"),  # query 'ch'
        10,  # exit search mode / confirm search
        10,  # ENTER select
    ]

    selected = _curses_radiolist_screen(mock_stdscr, "Select Fruit", items)
    # Filtered to Cherry, which is original index 2
    assert selected == 2


def test_mock_curses_quit_and_back():
    items = [MenuItem(label="Item 1"), MenuItem(label="Item 2")]

    # 'q' raises MenuCancelled
    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [ord("q")]
    with pytest.raises(MenuCancelled):
        _curses_radiolist_screen(mock_stdscr, "Test", items)

    # KEY_LEFT raises MenuGoBack when allow_back=True
    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [curses.KEY_LEFT]
    with pytest.raises(MenuGoBack):
        _curses_radiolist_screen(mock_stdscr, "Test", items, allow_back=True)


def test_curses_radiolist_empty_options_raises():
    with pytest.raises(InstallError, match="No options available to select"):
        curses_radiolist("Empty", [])


def test_cli_main_keyboard_interrupt_handling():
    from services.distribution import cli

    # Simulate KeyboardInterrupt in dispatch
    with patch("services.distribution.cli.dispatch", side_effect=KeyboardInterrupt):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["setup"])
        assert exc_info.value.code == 1


def test_curses_radiolist_eof_spin_guard_raises_instead_of_hanging():
    """Issue #543: repeated getch() == -1 (piped stdin EOF) must abort instead of spinning."""
    items = [MenuItem(label="Workspace Default"), MenuItem(label="Workspace CWD")]

    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.return_value = -1

    with pytest.raises(MenuCancelled, match="EOF"):
        _curses_radiolist_screen(mock_stdscr, "Select Authorized Workspace", items)
    assert mock_stdscr.getch.call_count == 32

    mock_stdscr_check = MagicMock()
    mock_stdscr_check.getmaxyx.return_value = (24, 80)
    mock_stdscr_check.getch.return_value = -1
    with pytest.raises(MenuCancelled, match="EOF"):
        _curses_checklist_screen(mock_stdscr_check, "Select Services", items)
    assert mock_stdscr_check.getch.call_count == 32


def test_ensure_stdin_tty_rebinds_fd_zero_when_stdin_is_pipe(monkeypatch: pytest.MonkeyPatch):
    """Issue #543: _ensure_stdin_tty binds fd 0 to active_tty.fileno() when stdin is piped."""
    from services.distribution.curses_menu import _ensure_stdin_tty

    dup2_calls: list[tuple[int, int]] = []

    class _FakeTTYWithFD:
        def fileno(self) -> int:
            return 7

    monkeypatch.setattr("os.isatty", lambda fd: fd == 7)
    monkeypatch.setattr("os.dup", lambda fd: 99)
    monkeypatch.setattr("os.dup2", lambda src, dst: dup2_calls.append((src, dst)))
    monkeypatch.setattr("os.close", lambda fd: None)

    with _ensure_stdin_tty(_FakeTTYWithFD()):
        assert dup2_calls == [(7, 0)]
    assert dup2_calls == [(7, 0), (99, 0)]


def test_curses_radiolist_selectable_previous_step_and_previous_keyword():
    """Issue #545: allow_back=True appends selectable '← Previous step' item and accepts 'previous'."""
    items = [MenuItem(label="Option 1"), MenuItem(label="Option 2")]

    # Selecting the appended '← Previous step' item (index 2) raises MenuGoBack
    mock_stdscr = MagicMock()
    mock_stdscr.getmaxyx.return_value = (24, 80)
    mock_stdscr.getch.side_effect = [
        curses.KEY_DOWN,  # move to Option 2 (idx 1)
        curses.KEY_DOWN,  # move to ← Previous step (idx 2)
        10,  # ENTER
    ]
    with pytest.raises(MenuGoBack):
        _curses_radiolist_screen(mock_stdscr, "Select Provider", items, allow_back=True)

    # Line fallback accepts 'previous' and 'prev'
    for keyword in ("previous", "prev", "back", ":back", "<"):
        tty = _FakeTTY([keyword])
        with pytest.raises(MenuGoBack):
            curses_radiolist("Select Provider", items, allow_back=True, tty=tty)
