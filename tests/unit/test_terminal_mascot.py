"""Issue #386: deterministic approved Kinaiya terminal mascot and width-aware fallbacks."""

from __future__ import annotations

import pytest

from services.distribution import mascot as mascot_module
from services.distribution import wizard
from services.distribution.mascot import (
    MASCOT_ASCII,
    MASCOT_COMPACT,
    MASCOT_COMPACT_ASCII,
    MASCOT_FULL,
    MASCOT_WORDMARK,
    MASCOT_WORDMARK_ASCII,
    render_mascot,
)


class FakeTTY:
    """Stub terminal object with controllable tty, encoding, and column dimensions."""

    def __init__(
        self,
        *,
        tty: bool = True,
        encoding: str = "utf-8",
        columns: int | None = None,
    ) -> None:
        self._tty = tty
        self.encoding = encoding
        self.columns = columns

    def isatty(self) -> bool:
        return self._tty


def test_mascot_static_variants_fit_declared_column_bounds_and_preserve_brand() -> None:
    assert "Kinaiya" in MASCOT_FULL
    assert "(" in MASCOT_FULL and "~" in MASCOT_FULL and "*" in MASCOT_FULL
    assert max(len(line) for line in MASCOT_FULL.splitlines()) <= 72
    assert max(len(line) for line in MASCOT_FULL.splitlines()) <= 64

    assert "Kinaiya" in MASCOT_ASCII
    assert all(ord(ch) < 128 for ch in MASCOT_ASCII)
    assert max(len(line) for line in MASCOT_ASCII.splitlines()) <= 64

    assert "Kinaiya" in MASCOT_COMPACT
    assert max(len(line) for line in MASCOT_COMPACT.splitlines()) <= 38

    assert "Kinaiya" in MASCOT_COMPACT_ASCII
    assert all(ord(ch) < 128 for ch in MASCOT_COMPACT_ASCII)
    assert max(len(line) for line in MASCOT_COMPACT_ASCII.splitlines()) <= 38

    assert MASCOT_WORDMARK == "Kinaiya — Local-First Companion & Agent Host"
    assert all(ord(ch) < 128 for ch in MASCOT_WORDMARK_ASCII)


@pytest.mark.parametrize("width", [120, 80, 50, 32])
def test_render_mascot_respects_terminal_width_without_line_overflow(width: int) -> None:
    rendered = render_mascot(
        width=width,
        unicode_ok=True,
        is_tty=True,
        env={"LANG": "en_US.UTF-8", "TERM": "xterm-256color"},
    )
    assert "Kinaiya" in rendered
    for line in rendered.splitlines():
        assert len(line) <= width

    if width >= 64:
        assert rendered == MASCOT_FULL
    elif width >= 40:
        assert rendered == MASCOT_COMPACT
    else:
        assert "Local-First Companion" in rendered


@pytest.mark.parametrize("width", [120, 80, 50, 32])
def test_render_mascot_ascii_fallback_for_lang_c_and_non_utf8_streams(width: int) -> None:
    rendered = render_mascot(
        width=width,
        tty=FakeTTY(tty=True, encoding="utf-8"),
        env={"LANG": "C", "TERM": "xterm-256color"},
    )
    assert "Kinaiya" in rendered
    assert all(ord(ch) < 128 for ch in rendered)
    for line in rendered.splitlines():
        assert len(line) <= width

    ascii_stream_rendered = render_mascot(
        width=width,
        tty=FakeTTY(tty=True, encoding="ascii"),
        env={"LANG": "en_US.UTF-8", "TERM": "xterm-256color"},
    )
    assert all(ord(ch) < 128 for ch in ascii_stream_rendered)


def test_render_mascot_dumb_terminal_non_tty_and_no_color_have_zero_control_residue() -> None:
    dumb_out = render_mascot(
        tty=FakeTTY(tty=True, columns=80),
        env={"TERM": "dumb", "LANG": "en_US.UTF-8"},
    )
    assert dumb_out == MASCOT_WORDMARK_ASCII
    assert "\x1b" not in dumb_out

    non_tty_out = render_mascot(
        tty=FakeTTY(tty=False, columns=80),
        env={"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
    )
    assert non_tty_out == MASCOT_WORDMARK
    assert "\x1b" not in non_tty_out

    no_color_out = render_mascot(
        tty=FakeTTY(tty=True, columns=80),
        env={"TERM": "xterm-256color", "NO_COLOR": "1", "LANG": "en_US.UTF-8"},
    )
    assert no_color_out == MASCOT_FULL
    assert "\x1b" not in no_color_out
    assert "\x1b[?25l" not in no_color_out


def test_render_mascot_is_deterministic_and_wizard_reexports_mascot() -> None:
    first = render_mascot(width=80, unicode_ok=True, is_tty=True)
    second = render_mascot(width=80, unicode_ok=True, is_tty=True)
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")

    assert wizard.MASCOT == mascot_module.MASCOT
    assert "Kinaiya" in wizard.MASCOT
    assert wizard.mascot(width=80, interactive=False) == "Kinaiya"
    assert max(len(line) for line in wizard.mascot(width=12, interactive=True).splitlines()) <= 12
