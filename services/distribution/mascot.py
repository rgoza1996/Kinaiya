"""Deterministic terminal representations of the approved Kinaiya mascot.

Implements Issue #386:
- Checked-in static portrait variants adapted from assets/mascot/kinaiya-mascot.png
  (swept hair with crescent moon and four-point stars, serene profile, draped robe,
  and concentric water ripples underfoot).
- Width-aware, locale-aware, and terminal-capability-aware pure selector with zero
  runtime image/network dependencies and zero terminal control residue.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from collections.abc import Mapping
from typing import Any

MASCOT_WORDMARK = "Kinaiya — Local-First Companion & Agent Host"
MASCOT_WORDMARK_ASCII = "Kinaiya - Local-First Companion & Agent Host"

MASCOT_FULL = r"""                  . - ~ ~ - .
            . - ~   ☾ ( ✦ )   ~ - .
        . ~    *      ` - '    .    ~ .
      /     ✦       _ . - - . _  ( · _ )
     (    *    _ . '   _ . - '  \  / `
      \  ~ _ . '     /   /   _ . \/
        ` ~   _ . - /   /  /   /  \
          _ /      /   /  /   /    \ _
      < = =       /   / _/   /        = = >
              _ . '  / /    / \
          _ /    _ . ' /   /   \
         (   _ . '    /   /     \
          ` '        /  _ \      \
                    ( /   \ \     \
            ~ ~ ~  _ /     \ \ _   \
        . - ~ ( _ _ _ ) ~ ( _ _ _ ) ~ - .
      (  ~  ~   (  ~  ( _ _ )  ~  )   ~  )
           ~ ~ ✦  Kinaiya  *  ☾ ~ ~
     Kinaiya — Local-First Companion & Agent Host"""

MASCOT_ASCII = r"""                  . - ~ ~ - .
            . - ~   ( ( * )   ~ - .
        . ~    *      ` - '    .    ~ .
      /     *       _ . - - . _  ( . _ )
     (    *    _ . '   _ . - '  \  / `
      \  ~ _ . '     /   /   _ . \/
        ` ~   _ . - /   /  /   /  \
          _ /      /   /  /   /    \ _
      < = =       /   / _/   /        = = >
              _ . '  / /    / \
          _ /    _ . ' /   /   \
         (   _ . '    /   /     \
          ` '        /  _ \      \
                    ( /   \ \     \
            ~ ~ ~  _ /     \ \ _   \
        . - ~ ( _ _ _ ) ~ ( _ _ _ ) ~ - .
      (  ~  ~   (  ~  ( _ _ )  ~  )   ~  )
           ~ ~ *  Kinaiya  *  ( ~ ~
     Kinaiya - Local-First Companion & Agent Host"""

MASCOT_COMPACT = r"""         . - ~ ☾ ( ✦ ) ~ .
      ~ *   _ . - ' \ ( · _ )
       ` ~ /   /  /  \/
     < =  /   / _/ \   = >
         ( _ '  /   \
        ~ ~ ( _ _ ) ~ ( _ _ ) ~
          ~ ✦ Kinaiya * ☾ ~"""

MASCOT_COMPACT_ASCII = r"""         . - ~ ( ( * ) ~ .
      ~ *   _ . - ' \ ( . _ )
       ` ~ /   /  /  \/
     < =  /   / _/ \   = >
         ( _ '  /   \
        ~ ~ ( _ _ ) ~ ( _ _ ) ~
          ~ * Kinaiya * ( ~"""

MASCOT = MASCOT_FULL


def _detect_unicode(
    *,
    tty: Any = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    active_env = os.environ if env is None else env
    if active_env.get("KINAIYA_ASCII_ONLY", "") != "":
        return False
    if active_env.get("TERM", "").strip().lower() == "dumb":
        return False

    encoding = getattr(tty, "encoding", None) if tty is not None else None
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


def _detect_is_tty(
    *,
    tty: Any = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    active_env = os.environ if env is None else env
    if active_env.get("TERM", "").strip().lower() == "dumb":
        return False
    if tty is not None:
        isatty = getattr(tty, "isatty", None)
        if callable(isatty):
            try:
                return bool(isatty())
            except Exception:
                return False
        return True
    return True


def _detect_width(
    *,
    tty: Any = None,
    env: Mapping[str, str] | None = None,
    default: int = 80,
) -> int:
    active_env = os.environ if env is None else env
    raw_columns = active_env.get("COLUMNS", "").strip()
    if raw_columns:
        try:
            parsed = int(raw_columns)
            if parsed > 0:
                return parsed
        except ValueError:
            pass

    if tty is not None:
        columns_attr = getattr(tty, "columns", None)
        if isinstance(columns_attr, int) and columns_attr > 0:
            return columns_attr
        fileno = getattr(tty, "fileno", None)
        if callable(fileno):
            try:
                cols = os.get_terminal_size(fileno()).columns
                if cols > 0:
                    return cols
            except Exception:
                pass
        return default

    if env is None:
        return shutil.get_terminal_size(fallback=(default, 24)).columns
    return default


def _fit_wordmark(wordmark: str, width: int) -> str:
    if width <= 0:
        return ""
    if width < 16:
        return "Kinaiya"[:width]
    if len(wordmark) <= width:
        return wordmark
    return textwrap.fill(
        wordmark,
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
    )


def render_mascot(
    *,
    width: int | None = None,
    unicode_ok: bool | None = None,
    is_tty: bool | None = None,
    env: Mapping[str, str] | None = None,
    tty: Any = None,
) -> str:
    """Return a deterministic, plain-text mascot representation for the target terminal."""
    effective_unicode = unicode_ok if unicode_ok is not None else _detect_unicode(tty=tty, env=env)
    effective_tty = is_tty if is_tty is not None else _detect_is_tty(tty=tty, env=env)
    effective_width = width if width is not None else _detect_width(tty=tty, env=env)

    wordmark = MASCOT_WORDMARK if effective_unicode else MASCOT_WORDMARK_ASCII

    if not effective_tty or effective_width < 40:
        return _fit_wordmark(wordmark, effective_width)

    if effective_width >= 64:
        return MASCOT_FULL if effective_unicode else MASCOT_ASCII

    return MASCOT_COMPACT if effective_unicode else MASCOT_COMPACT_ASCII
