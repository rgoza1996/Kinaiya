"""Issue #385: shared Kinaiya terminal palette, prompt semantics, and accessible fallbacks."""

from __future__ import annotations

import pytest

from services.distribution import terminal_ui
from services.distribution.terminal_ui import (
    PALETTE,
    ColorMode,
    TerminalTheme,
    assert_no_ansi,
    choice_item,
    command_hint,
    detect_color_mode,
    detect_width,
    format_json_safe,
    heading,
    muted,
    prompt_label,
    section_banner,
    status_line,
    strip_ansi,
    supports_unicode,
    visible_len,
    wrap_text,
)


class FakeStream:
    """Controllable stream stub for terminal capability testing."""

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


def test_approved_kinaiya_palette_rgb_and_truecolor_sequences() -> None:
    assert PALETTE.indigo.hex_code == "#6366F1"
    assert PALETTE.indigo.rgb == (99, 102, 241)
    assert PALETTE.moonlight.hex_code == "#E0E7FF"
    assert PALETTE.moonlight.rgb == (224, 231, 255)
    assert PALETTE.slate.hex_code == "#94A3B8"
    assert PALETTE.slate.rgb == (148, 163, 184)
    assert PALETTE.emerald.hex_code == "#10B981"
    assert PALETTE.emerald.rgb == (16, 185, 129)
    assert PALETTE.amber.hex_code == "#F59E0B"
    assert PALETTE.amber.rgb == (245, 158, 11)
    assert PALETTE.rose.hex_code == "#F43F5E"
    assert PALETTE.rose.rgb == (244, 63, 94)

    assert PALETTE.indigo.fg_sequence(ColorMode.TRUECOLOR) == "\x1b[38;2;99;102;241m"
    assert PALETTE.emerald.fg_sequence(ColorMode.TRUECOLOR) == "\x1b[38;2;16;185;129m"
    assert PALETTE.indigo.fg_sequence(ColorMode.ANSI16) == "\x1b[94m"
    assert PALETTE.indigo.fg_sequence(ColorMode.NONE) == ""


@pytest.mark.parametrize(
    "tty,env,expected",
    [
        (True, {"TERM": "xterm-256color", "COLORTERM": "truecolor"}, ColorMode.TRUECOLOR),
        (True, {"TERM": "xterm-256color", "COLORTERM": "24bit"}, ColorMode.TRUECOLOR),
        (True, {"TERM": "xterm-256color"}, ColorMode.ANSI16),
        (
            True,
            {"TERM": "xterm-256color", "COLORTERM": "truecolor", "NO_COLOR": "1"},
            ColorMode.NONE,
        ),
        (True, {"TERM": "xterm-256color", "KINAIYA_NO_COLOR": "true"}, ColorMode.NONE),
        (True, {"TERM": "dumb", "COLORTERM": "truecolor"}, ColorMode.NONE),
        (False, {"TERM": "xterm-256color", "COLORTERM": "truecolor"}, ColorMode.NONE),
    ],
)
def test_detect_color_mode_honors_no_color_dumb_and_non_tty(
    tty: bool, env: dict[str, str], expected: ColorMode
) -> None:
    stream = FakeStream(tty=tty)
    assert detect_color_mode(stream, env) == expected


@pytest.mark.parametrize(
    "encoding,env,expected",
    [
        ("utf-8", {"LANG": "en_US.UTF-8", "TERM": "xterm-256color"}, True),
        ("UTF-8", {"LC_ALL": "C.UTF-8"}, True),
        ("utf-8", {"LANG": "C"}, False),
        ("utf-8", {"LC_ALL": "POSIX", "LANG": "en_US.UTF-8"}, False),
        ("ascii", {"LANG": "en_US.UTF-8"}, False),
        ("utf-8", {"LANG": "en_US.UTF-8", "TERM": "dumb"}, False),
        ("utf-8", {"LANG": "en_US.UTF-8", "KINAIYA_ASCII_ONLY": "1"}, False),
    ],
)
def test_supports_unicode_falls_back_to_ascii_when_locale_or_encoding_restricted(
    encoding: str, env: dict[str, str], expected: bool
) -> None:
    stream = FakeStream(tty=True, encoding=encoding)
    assert supports_unicode(stream, env) is expected


def test_detect_width_respects_columns_and_clamps_to_safe_bounds() -> None:
    stream = FakeStream(tty=True)
    assert detect_width(stream, {"COLUMNS": "120"}) == 120
    assert detect_width(stream, {"COLUMNS": "80"}) == 80
    assert detect_width(stream, {"COLUMNS": "18"}) == 40
    assert detect_width(stream, {"COLUMNS": "999"}) == 240
    assert detect_width(stream, {"COLUMNS": "not-a-number"}, default=80) == 80
    assert detect_width(FakeStream(tty=True, columns=64), {}) == 64


@pytest.mark.parametrize("width", [40, 80, 120])
def test_wrap_text_preserves_indentation_and_never_splits_ansi_sequences(width: int) -> None:
    theme = TerminalTheme(
        stream=FakeStream(tty=True),
        env={"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"},
        width=width,
    )
    colored_sentence = (
        f"{theme.paint('Kinaiya', PALETTE.indigo, bold=True)} keeps "
        f"{theme.paint('Agent', PALETTE.moonlight)} workspace boundaries isolated from "
        f"{theme.paint('trusted host state', PALETTE.emerald)} and never executes tasks during setup."
    )
    wrapped = theme.wrap(colored_sentence, width=width, indent="  ", subsequent_indent="    ")
    lines = wrapped.splitlines()
    assert len(lines) >= 1
    for idx, line in enumerate(lines):
        assert visible_len(line) <= width
        expected_prefix = "  " if idx == 0 else "    "
        assert strip_ansi(line).startswith(expected_prefix)
        # Ensure no dangling or split CSI sequence exists on any line.
        stripped = strip_ansi(line)
        assert "\x1b" not in stripped


def test_long_token_wrapping_closes_and_reopens_ansi_cleanly() -> None:
    theme = TerminalTheme(
        stream=FakeStream(tty=True),
        env={"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"},
        width=40,
    )
    url = "https://provider.example.internal/v1/models/super-long-identifier-path-segment"
    colored_url = theme.paint(url, PALETTE.indigo)
    wrapped = theme.wrap(colored_url, width=40)
    lines = wrapped.splitlines()
    assert len(lines) >= 2
    for line in lines:
        assert visible_len(line) <= 40
        assert "\x1b" not in strip_ansi(line)
        assert line.endswith("\x1b[0m")
    assert "".join(strip_ansi(line) for line in lines) == url


def test_status_line_always_includes_non_color_semantic_prefixes_across_all_modes() -> None:
    modes = [
        TerminalTheme(
            stream=FakeStream(tty=True),
            env={"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"},
        ),
        TerminalTheme(
            stream=FakeStream(tty=True),
            env={"TERM": "xterm-256color", "LANG": "en_US.UTF-8"},
        ),
        TerminalTheme(
            stream=FakeStream(tty=True),
            env={"TERM": "xterm-256color", "NO_COLOR": "1", "LANG": "en_US.UTF-8"},
        ),
        TerminalTheme(
            stream=FakeStream(tty=False),
            env={"TERM": "dumb", "LANG": "C"},
        ),
    ]
    expected_prefixes = {
        "success": "[OK]",
        "warning": "[WARN]",
        "error": "[ERR]",
        "info": "[INFO]",
        "configured_untested": "[UNTESTED]",
        "verified": "[VERIFIED]",
    }
    for theme in modes:
        for kind, expected_tag in expected_prefixes.items():
            rendered = theme.status_line(
                kind,
                "Provider endpoint recorded",
                detail="connection not probed",
            )
            plain = strip_ansi(rendered)
            assert expected_tag in plain
            assert "Provider endpoint recorded" in plain
            assert "connection not probed" in plain
            if theme.color_mode == ColorMode.NONE:
                assert_no_ansi(rendered)
            if not theme.unicode_ok:
                assert all(ord(ch) < 128 for ch in rendered)

    with pytest.raises(ValueError, match="Unknown status kind"):
        modes[0].status_line("unknown_state", "bad kind")


def test_semantic_helpers_in_truecolor_16color_and_ascii_fallback() -> None:
    truecolor_theme = TerminalTheme(
        stream=FakeStream(tty=True),
        env={"TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8"},
        width=80,
    )
    ascii_theme = TerminalTheme(
        stream=FakeStream(tty=False),
        env={"TERM": "dumb", "LANG": "C", "NO_COLOR": "1"},
        width=80,
    )

    tc_heading = heading("Kinaiya Setup", subtitle="Local-first companion", theme=truecolor_theme)
    assert "\x1b[38;2;99;102;241m" in tc_heading
    assert "─" in tc_heading

    ascii_heading = heading("Kinaiya Setup", subtitle="Local-first companion", theme=ascii_theme)
    assert_no_ansi(ascii_heading)
    assert all(ord(ch) < 128 for ch in ascii_heading)
    assert "-------------" in ascii_heading

    tc_banner = section_banner(2, 4, "Agent Provider", theme=truecolor_theme)
    assert "[2/4]" in strip_ansi(tc_banner)
    assert "Agent Provider" in strip_ansi(tc_banner)

    with pytest.raises(ValueError, match="Invalid step bounds"):
        section_banner(5, 4, "Invalid", theme=ascii_theme)

    prompt = prompt_label(
        "Allow cloud disclosure",
        default="no",
        hint="Cloud providers receive authorized prompt context.",
        choices=("yes", "no"),
        theme=ascii_theme,
    )
    assert_no_ansi(prompt)
    assert "> Allow cloud disclosure (yes/no) [no]: " in prompt
    assert "Cloud providers receive authorized prompt context." in prompt

    opt = choice_item(
        1,
        "Local OpenAI-compatible server",
        description="Connect to LM Studio, llama.cpp, vLLM, or Ollama on localhost.",
        recommended=True,
        selected=True,
        theme=ascii_theme,
    )
    assert_no_ansi(opt)
    assert "> [1] Local OpenAI-compatible server (recommended)" in opt
    assert "Connect to LM Studio" in opt

    cmd = command_hint("kinaiya setup", description="Run interactive wizard", theme=ascii_theme)
    assert_no_ansi(cmd)
    assert "$ kinaiya setup  # Run interactive wizard" in cmd

    dimmed = muted("secondary detail", theme=ascii_theme)
    assert dimmed == "secondary detail"

    status = status_line("configured_untested", "Endpoint saved", theme=ascii_theme)
    assert "[UNTESTED] Endpoint saved" in status
    wrapped_plain = wrap_text("One two three four five", width=20, indent="  ")
    assert all(len(line) <= 20 for line in wrapped_plain.splitlines())


def test_json_and_log_safety_helpers_reject_escape_codes() -> None:
    payload = {
        "provider_connection": "not_checked",
        "ready": False,
        "next": "kinaiya setup; kinaiya serve host",
    }
    serialized = format_json_safe(payload)
    assert "\x1b" not in serialized
    assert assert_no_ansi(serialized) == serialized

    colored = terminal_ui.PALETTE.indigo.fg_sequence(ColorMode.TRUECOLOR) + "Kinaiya\x1b[0m"
    with pytest.raises(ValueError, match="Forbidden ANSI"):
        assert_no_ansi(colored)
    with pytest.raises(ValueError, match="Forbidden ANSI"):
        format_json_safe({"label": colored})
