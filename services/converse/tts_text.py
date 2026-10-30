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
_PRESENTATION_EMOJI = re.compile(
    r"[\u203c\u2049\u2139\u2194-\u2199\u21a9\u21aa\u2934\u2935\u3030\u303d]\ufe0f"
)
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
    # These emoji-presentation bases have letter/punctuation/math categories.
    # Their ordinary text forms stay intact; VS16 explicitly selects the emoji.
    text = _PRESENTATION_EMOJI.sub(" ", text)
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
        elif char == "\u200d" and not (
            index > 0
            and index + 1 < len(text)
            and unicodedata.category(text[index - 1])[0] in "LM"
            and unicodedata.category(text[index + 1])[0] in "LM"
        ):
            continue
        else:
            result.append(char)
    cleaned = " ".join("".join(result).split())
    # Punctuation and invisible remnants alone are not an utterance. Keep
    # punctuation inside real speech without asking a backend to name it.
    return cleaned if any(char.isalnum() for char in cleaned) else ""
