import pytest

from services.converse.tts_text import normalize_for_tts


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello 😊 How are you?", "Hello How are you?"),
        ("One🙂two", "One two"),
        ("👩🏽‍💻 👨‍👩‍👧‍👦 🇵🇭 1️⃣ ❤️ ★ •", ""),
        ("Sure :) I'll think about it. ;-) :D", "Sure I'll think about it."),
        (
            "12:30 http://example.test/a:=b x:=3 C++ C#",
            "12:30 http://example.test/a:=b x:=3 C++ C#",
        ),
        ("It is -2.5°C, €10 + $2; x≥3 and 2*4=8.", "It is -2.5°C, €10 + $2; x≥3 and 2*4=8."),
        ("24°C 75℉ 21℃ 1 2 3", "24°C 75℉ 21℃ 1 2 3"),
        ("Café e\u0301 日本語 العربية می\u200dروم", "Café e\u0301 日本語 العربية می\u200dروم"),
        ("## Hello **friend**\n* Keep `hello` readable.", "Hello friend Keep hello readable."),
        ("Say smiley face, if those words matter.", "Say smiley face, if those words matter."),
        (" \n\t ", ""),
    ],
)
def test_speech_cleanup_keeps_language_and_meaning(text, expected):
    assert normalize_for_tts(text) == expected
