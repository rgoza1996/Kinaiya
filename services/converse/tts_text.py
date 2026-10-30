"""Speech-only lexical cleanup; retained dialogue is never rewritten.

Remove pictographic/decorative symbols and standalone smileys without naming
them aloud. Keep letters, combining accents, numbers, currency, mathematical
operators, punctuation and temperature units. This is not a Markdown renderer
or a phonetic normalizer. Literal words such as 'smiley face' are preserved.
"""

import re
import unicodedata

_UNITS = frozenset("°℃℉")
_KEYCAP = re.compile(r"[0-9#*]\ufe0f?\u20e3")
_SMILEY = re.compile(r"(?<![\w:/=])[:;=](?:[-^])?[)\]([dDpP](?![\w:/=])")
_MARKERS = re.compile(r"(?m)^ {0,3}(?:#{1,6}\s+|[*]\s+)")
_PAIRED = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`")


def _decoration(char):
    return (
        (unicodedata.category(char) == "So" and char not in _UNITS)
        or char in "•‣⁃"
        or 0x1F3FB <= ord(char) <= 0x1F3FF
    )


def normalize_for_tts(text: str) -> str:
    """Return synthesizer text; removed glyphs separate words rather than join.

    Regional flags, skin modifiers and emoji bases are Unicode symbol-other.
    Strip their selectors, tags and joiners too. Preserve a joiner between real
    letters, where it can affect spelling/shaping. Keycaps are removed as whole
    emoji sequences, leaving ordinary digits intact. Ambiguous code/math stays.
    """
    text = _KEYCAP.sub(" ", text)
    text = _SMILEY.sub(" ", text)
    text = _MARKERS.sub("", text)
    text = _PAIRED.sub(lambda match: next(x for x in match.groups() if x is not None), text)
    result = []
    for index, char in enumerate(text):
        number = ord(char)
        if _decoration(char):
            result.append(" ")
        elif number in {0xFE0E, 0xFE0F, 0x20E3} or 0xE0020 <= number <= 0xE007F:
            continue
        elif char == "\u200d" and (
            (index > 0 and _decoration(text[index - 1]))
            or (index + 1 < len(text) and _decoration(text[index + 1]))
        ):
            continue
        else:
            result.append(char)
    return " ".join("".join(result).split())
