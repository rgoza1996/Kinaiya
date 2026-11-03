"""Zero-dependency terminal presentation helpers, Kinaiya palette, and accessible fallbacks.

Implements Issue #385:
- Approved Kinaiya terminal color palette with 24-bit truecolor, 16-color ANSI, and plain-text
  fallbacks honoring NO_COLOR, KINAIYA_NO_COLOR, TERM=dumb, and non-TTY streams.
- Locale-aware Unicode vs 7-bit ASCII symbol fallback.
- Safe width detection and ANSI-preserving line wrapping.
- Semantic prompt, banner, choice, command hint, and non-color-reliant status helpers.
- ANSI stripping and JSON/log output safety checks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


class ColorMode(StrEnum):
    """Terminal color capability mode."""

    NONE = "none"
    ANSI16 = "ansi16"
    TRUECOLOR = "truecolor"


@dataclass(frozen=True)
class ColorToken:
    """Single semantic color entry in the Kinaiya palette."""

    name: str
    hex_code: str
    rgb: tuple[int, int, int]
    ansi16: str

    def fg_sequence(self, mode: ColorMode) -> str:
        if mode == ColorMode.TRUECOLOR:
            r, g, b = self.rgb
            return f"\x1b[38;2;{r};{g};{b}m"
        if mode == ColorMode.ANSI16:
            return self.ansi16
        return ""


@dataclass(frozen=True)
class KinaiyaPalette:
    """Approved Kinaiya terminal color palette."""

    indigo: ColorToken = ColorToken(
        name="indigo",
        hex_code="#6366F1",
        rgb=(99, 102, 241),
        ansi16="\x1b[94m",
    )
    moonlight: ColorToken = ColorToken(
        name="moonlight",
        hex_code="#E0E7FF",
        rgb=(224, 231, 255),
        ansi16="\x1b[97m",
    )
    slate: ColorToken = ColorToken(
        name="slate",
        hex_code="#94A3B8",
        rgb=(148, 163, 184),
        ansi16="\x1b[90m",
    )
    emerald: ColorToken = ColorToken(
        name="emerald",
        hex_code="#10B981",
        rgb=(16, 185, 129),
        ansi16="\x1b[32m",
    )
    amber: ColorToken = ColorToken(
        name="amber",
        hex_code="#F59E0B",
        rgb=(245, 158, 11),
        ansi16="\x1b[33m",
    )
    rose: ColorToken = ColorToken(
        name="rose",
        hex_code="#F43F5E",
        rgb=(244, 63, 94),
        ansi16="\x1b[31m",
    )


PALETTE = KinaiyaPalette()


@dataclass(frozen=True)
class SymbolSet:
    """Semantic glyphs with deterministic Unicode and 7-bit ASCII representations."""

    success: str
    warning: str
    error: str
    info: str
    untested: str
    verified: str
    pointer: str
    rule: str
    bullet: str


UNICODE_SYMBOLS = SymbolSet(
    success="✓",
    warning="⚠",
    error="✗",
    info="•",
    untested="!",
    verified="✓",
    pointer="›",
    rule="─",
    bullet="•",
)

ASCII_SYMBOLS = SymbolSet(
    success="[OK]",
    warning="[WARN]",
    error="[ERR]",
    info="[INFO]",
    untested="[UNTESTED]",
    verified="[VERIFIED]",
    pointer=">",
    rule="-",
    bullet="*",
)

STATUS_LABELS: dict[str, str] = {
    "success": "[OK]",
    "warning": "[WARN]",
    "error": "[ERR]",
    "info": "[INFO]",
    "configured_untested": "[UNTESTED]",
    "verified": "[VERIFIED]",
}


def _stream_is_tty(stream: Any) -> bool:
    if stream is None:
        return False
    isatty = getattr(stream, "isatty", None)
    if callable(isatty):
        try:
            return bool(isatty())
        except Exception:
            return False
    return False


def detect_color_mode(
    stream: Any = None,
    env: Mapping[str, str] | None = None,
) -> ColorMode:
    """Determine terminal color support while honoring NO_COLOR and TERM=dumb."""
    active_env = os.environ if env is None else env
    if active_env.get("NO_COLOR", "") != "" or active_env.get("KINAIYA_NO_COLOR", "") != "":
        return ColorMode.NONE
    term = active_env.get("TERM", "").strip().lower()
    if term == "dumb":
        return ColorMode.NONE
    if not _stream_is_tty(stream):
        return ColorMode.NONE
    colorterm = active_env.get("COLORTERM", "").strip().lower()
    if colorterm in {"truecolor", "24bit"}:
        return ColorMode.TRUECOLOR
    return ColorMode.ANSI16


def supports_unicode(
    stream: Any = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True only when both terminal locale and stream encoding support UTF-8."""
    active_env = os.environ if env is None else env
    if active_env.get("KINAIYA_ASCII_ONLY", "") != "":
        return False
    if active_env.get("TERM", "").strip().lower() == "dumb":
        return False

    encoding = getattr(stream, "encoding", None) if stream is not None else None
    if isinstance(encoding, str) and encoding:
        if "UTF" not in encoding.upper():
            return False

    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = active_env.get(key, "").strip()
        if not val:
            continue
        upper = val.upper()
        if upper in {"C", "POSIX"} or upper.startswith(("C.", "POSIX.")):
            if "UTF-8" not in upper and "UTF8" not in upper:
                return False
        if "UTF-8" in upper or "UTF8" in upper:
            return True
        return False

    if isinstance(encoding, str) and encoding:
        return "UTF" in encoding.upper()
    return True


def detect_width(
    stream: Any = None,
    env: Mapping[str, str] | None = None,
    default: int = 80,
    min_width: int = 40,
    max_width: int = 240,
) -> int:
    """Detect terminal column width with safe clamping."""
    active_env = os.environ if env is None else env
    raw_columns = active_env.get("COLUMNS", "").strip()
    if raw_columns:
        try:
            parsed = int(raw_columns)
            if parsed > 0:
                return max(min_width, min(parsed, max_width))
        except ValueError:
            pass

    if stream is not None:
        fileno = getattr(stream, "fileno", None)
        if callable(fileno):
            try:
                cols = os.get_terminal_size(fileno()).columns
                if cols > 0:
                    return max(min_width, min(cols, max_width))
            except Exception:
                pass
        columns_attr = getattr(stream, "columns", None)
        if isinstance(columns_attr, int) and columns_attr > 0:
            return max(min_width, min(columns_attr, max_width))
        return max(min_width, min(default, max_width))

    if env is None:
        cols = shutil.get_terminal_size(fallback=(default, 24)).columns
        return max(min_width, min(cols, max_width))

    return max(min_width, min(default, max_width))


def strip_ansi(text: str) -> str:
    """Remove all ANSI CSI escape sequences from text."""
    return ANSI_ESCAPE_RE.sub("", text)


def visible_len(text: str) -> int:
    """Return printable character length ignoring ANSI escape sequences."""
    return len(strip_ansi(text))


def assert_no_ansi(text: str) -> str:
    """Assert that text contains no ANSI escape sequences or unexpected control characters."""
    lower = text.lower()
    if (
        "\x1b" in text
        or "\\u001b" in lower
        or "\\x1b" in lower
        or ANSI_ESCAPE_RE.search(text)
        or CONTROL_CHAR_RE.search(text)
    ):
        raise ValueError("Forbidden ANSI escape or control sequence detected in plain/JSON output")
    return text


def format_json_safe(payload: Any, *, indent: int = 2) -> str:
    """Serialize payload as JSON and verify no terminal escape sequences leaked into it."""
    serialized = json.dumps(payload, indent=indent)
    return assert_no_ansi(serialized)


def _split_long_plain_token(token: str, limit: int) -> list[str]:
    """Split a long token without ever slicing inside an ANSI escape sequence."""
    if limit <= 0 or visible_len(token) <= limit:
        return [token]
    parts: list[str] = []
    current: list[str] = []
    current_vis = 0
    active_codes: list[str] = []
    idx = 0
    while idx < len(token):
        match = ANSI_ESCAPE_RE.match(token, idx)
        if match:
            seq = match.group(0)
            current.append(seq)
            if seq == RESET:
                active_codes.clear()
            else:
                active_codes.append(seq)
            idx = match.end()
            continue
        if current_vis >= limit:
            if active_codes:
                current.append(RESET)
            parts.append("".join(current))
            current = list(active_codes)
            current_vis = 0
        current.append(token[idx])
        current_vis += 1
        idx += 1
    if current:
        parts.append("".join(current))
    return parts


def wrap_text(
    text: str,
    width: int = 80,
    *,
    indent: str = "",
    subsequent_indent: str | None = None,
) -> str:
    """Wrap text to width while preserving indentation and keeping ANSI sequences intact."""
    effective_width = max(20, width)
    next_indent = indent if subsequent_indent is None else subsequent_indent
    paragraphs = text.split("\n")
    wrapped_lines: list[str] = []

    for paragraph in paragraphs:
        if not paragraph.strip():
            wrapped_lines.append("")
            continue
        words = paragraph.split(" ")
        current_indent = indent
        current_tokens: list[str] = []
        current_len = visible_len(current_indent)

        for word in words:
            if word == "":
                continue
            word_len = visible_len(word)
            avail = max(1, effective_width - visible_len(current_indent))
            if word_len > avail:
                if current_tokens:
                    wrapped_lines.append(current_indent + " ".join(current_tokens))
                    current_tokens = []
                    current_indent = next_indent
                    avail = max(1, effective_width - visible_len(current_indent))
                chunks = _split_long_plain_token(word, avail)
                for chunk in chunks[:-1]:
                    wrapped_lines.append(current_indent + chunk)
                    current_indent = next_indent
                current_tokens = [chunks[-1]]
                current_len = visible_len(current_indent) + visible_len(chunks[-1])
                continue

            sep_len = 1 if current_tokens else 0
            if current_tokens and (current_len + sep_len + word_len > effective_width):
                wrapped_lines.append(current_indent + " ".join(current_tokens))
                current_indent = next_indent
                current_tokens = [word]
                current_len = visible_len(current_indent) + word_len
            else:
                current_tokens.append(word)
                current_len += sep_len + word_len

        if current_tokens:
            wrapped_lines.append(current_indent + " ".join(current_tokens))

    return "\n".join(wrapped_lines)


class TerminalTheme:
    """Deterministic terminal presentation helper bound to terminal capabilities."""

    def __init__(
        self,
        *,
        stream: Any = None,
        env: Mapping[str, str] | None = None,
        color_mode: ColorMode | None = None,
        unicode_ok: bool | None = None,
        width: int | None = None,
        palette: KinaiyaPalette = PALETTE,
    ) -> None:
        self.stream = stream
        self.env = env
        self.palette = palette
        self.color_mode = (
            color_mode if color_mode is not None else detect_color_mode(stream=stream, env=env)
        )
        self.unicode_ok = (
            unicode_ok if unicode_ok is not None else supports_unicode(stream=stream, env=env)
        )
        self.width = width if width is not None else detect_width(stream=stream, env=env)
        self.symbols = UNICODE_SYMBOLS if self.unicode_ok else ASCII_SYMBOLS

    def paint(self, text: str, token: ColorToken, *, bold: bool = False) -> str:
        """Apply semantic color token if enabled, always closing with RESET."""
        if self.color_mode == ColorMode.NONE or not text:
            return text
        prefix = token.fg_sequence(self.color_mode)
        if bold:
            prefix = BOLD + prefix
        return f"{prefix}{text}{RESET}"

    def muted(self, text: str) -> str:
        """Render secondary/muted slate text."""
        return self.paint(text, self.palette.slate)

    def wrap(
        self,
        text: str,
        *,
        width: int | None = None,
        indent: str = "",
        subsequent_indent: str | None = None,
    ) -> str:
        """Wrap text to the theme's detected width (or explicit override)."""
        return wrap_text(
            text,
            width=self.width if width is None else width,
            indent=indent,
            subsequent_indent=subsequent_indent,
        )

    def heading(self, title: str, *, subtitle: str | None = None) -> str:
        """Render a prominent primary heading with an optional muted subtitle."""
        styled_title = self.paint(title, self.palette.indigo, bold=True)
        rule_len = min(self.width, max(8, visible_len(title)))
        rule = self.muted(self.symbols.rule * rule_len)
        lines = [styled_title, rule]
        if subtitle:
            lines.append(self.wrap(self.muted(subtitle), width=self.width))
        return "\n".join(lines)

    def section_banner(self, step_num: int, total_steps: int, title: str) -> str:
        """Render a numbered wizard section banner."""
        if step_num < 1 or total_steps < 1 or step_num > total_steps:
            raise ValueError(f"Invalid step bounds: {step_num}/{total_steps}")
        badge = self.paint(f"[{step_num}/{total_steps}]", self.palette.indigo, bold=True)
        heading_text = self.paint(title, self.palette.moonlight, bold=True)
        header = f"{badge} {heading_text}"
        rule_len = min(self.width, max(12, visible_len(header)))
        rule = self.muted(self.symbols.rule * rule_len)
        return f"{header}\n{rule}"

    def prompt_label(
        self,
        label: str,
        *,
        default: str | None = None,
        hint: str | None = None,
        choices: Sequence[str] | None = None,
    ) -> str:
        """Render a clear prompt label with optional choices, hint, and default value."""
        lines: list[str] = []
        if hint:
            lines.append(self.wrap(self.muted(f"  {hint}"), width=self.width, indent=""))
        pointer = self.paint(self.symbols.pointer, self.palette.indigo, bold=True)
        styled_label = self.paint(label, self.palette.moonlight, bold=True)
        parts = [f"{pointer} {styled_label}"]
        if choices:
            joined = "/".join(choices)
            parts.append(self.muted(f"({joined})"))
        if default is not None and default != "":
            parts.append(self.paint(f"[{default}]", self.palette.indigo))
        prompt_line = " ".join(parts) + ": "
        lines.append(prompt_line)
        return "\n".join(lines)

    def choice_item(
        self,
        index: int | str,
        label: str,
        *,
        description: str | None = None,
        recommended: bool = False,
        selected: bool = False,
    ) -> str:
        """Render an interactive or numbered choice option."""
        marker = self.symbols.pointer if selected else " "
        styled_marker = self.paint(marker, self.palette.indigo, bold=True) if selected else marker
        idx_badge = self.paint(f"[{index}]", self.palette.indigo, bold=selected)
        styled_label = self.paint(label, self.palette.moonlight, bold=True) if selected else label
        rec_tag = " " + self.paint("(recommended)", self.palette.emerald) if recommended else ""
        head = f" {styled_marker} {idx_badge} {styled_label}{rec_tag}"
        if not description:
            return head
        desc_wrapped = self.wrap(
            self.muted(description),
            width=self.width,
            indent="       ",
            subsequent_indent="       ",
        )
        return f"{head}\n{desc_wrapped}"

    def status_line(
        self,
        kind: str,
        message: str,
        *,
        detail: str | None = None,
    ) -> str:
        """Render a status line that never relies on color alone to convey meaning."""
        if kind not in STATUS_LABELS:
            raise ValueError(
                f"Unknown status kind {kind!r}; expected one of {sorted(STATUS_LABELS)}"
            )
        semantic_tag = STATUS_LABELS[kind]
        token_map: dict[str, ColorToken] = {
            "success": self.palette.emerald,
            "verified": self.palette.emerald,
            "warning": self.palette.amber,
            "configured_untested": self.palette.amber,
            "error": self.palette.rose,
            "info": self.palette.indigo,
        }
        glyph_map: dict[str, str] = {
            "success": self.symbols.success,
            "verified": self.symbols.verified,
            "warning": self.symbols.warning,
            "configured_untested": self.symbols.untested,
            "error": self.symbols.error,
            "info": self.symbols.info,
        }
        token = token_map[kind]
        glyph = glyph_map[kind]
        if self.unicode_ok and glyph != semantic_tag:
            prefix_raw = f"{glyph} {semantic_tag}"
        else:
            prefix_raw = semantic_tag
        prefix = self.paint(prefix_raw, token, bold=True)
        body = f"{prefix} {message}"
        if detail:
            body = (
                f"{body} — {self.muted(detail)}"
                if self.unicode_ok
                else f"{body} - {self.muted(detail)}"
            )
        return self.wrap(body, width=self.width, subsequent_indent="  ")

    def command_hint(self, command: str, *, description: str | None = None) -> str:
        """Render a runnable CLI command hint clearly distinguished from prose."""
        dollar = self.muted("$")
        cmd = self.paint(command, self.palette.moonlight, bold=True)
        if description:
            desc = self.muted(f"# {description}")
            return f"  {dollar} {cmd}  {desc}"
        return f"  {dollar} {cmd}"


def default_theme(
    *,
    stream: Any = None,
    env: Mapping[str, str] | None = None,
    width: int | None = None,
) -> TerminalTheme:
    """Construct a TerminalTheme from the given stream and environment."""
    return TerminalTheme(stream=stream, env=env, width=width)


def heading(
    title: str,
    *,
    subtitle: str | None = None,
    theme: TerminalTheme | None = None,
) -> str:
    active = theme or default_theme()
    return active.heading(title, subtitle=subtitle)


def section_banner(
    step_num: int,
    total_steps: int,
    title: str,
    *,
    theme: TerminalTheme | None = None,
) -> str:
    active = theme or default_theme()
    return active.section_banner(step_num, total_steps, title)


def prompt_label(
    label: str,
    *,
    default: str | None = None,
    hint: str | None = None,
    choices: Sequence[str] | None = None,
    theme: TerminalTheme | None = None,
) -> str:
    active = theme or default_theme()
    return active.prompt_label(label, default=default, hint=hint, choices=choices)


def choice_item(
    index: int | str,
    label: str,
    *,
    description: str | None = None,
    recommended: bool = False,
    selected: bool = False,
    theme: TerminalTheme | None = None,
) -> str:
    active = theme or default_theme()
    return active.choice_item(
        index,
        label,
        description=description,
        recommended=recommended,
        selected=selected,
    )


def muted(text: str, *, theme: TerminalTheme | None = None) -> str:
    active = theme or default_theme()
    return active.muted(text)


def status_line(
    kind: str,
    message: str,
    *,
    detail: str | None = None,
    theme: TerminalTheme | None = None,
) -> str:
    active = theme or default_theme()
    return active.status_line(kind, message, detail=detail)


def command_hint(
    command: str,
    *,
    description: str | None = None,
    theme: TerminalTheme | None = None,
) -> str:
    active = theme or default_theme()
    return active.command_hint(command, description=description)
