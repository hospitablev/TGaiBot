import pytest

from tgaibot.conversation import control_intent, simple_error
from tgaibot.media import MediaError
from tgaibot.provider import ProviderError
from tgaibot.tts import TTSError


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("Пожалуйста, отвечай голосом!", "/ai_voice_on"),
        ("Пиши текстом, пожалуйста.", "/ai_voice_off"),
        ("пиши еблан", "/ai_voice_off"),
        ("Пиши!", "/ai_voice_off"),
        ("Пиши текстом, блин", "/ai_voice_off"),
        ("Переведи: «пиши еблан»", "переведи: «пиши еблан»"),
        ("Пиши стих о море", "пиши стих о море"),
        ("Что ты умеешь?", "/ai_help"),
        ("Не отвечай мне", "/ai_stop"),
        ("Давай продолжим", "/ai_start"),
        ("Забудь нашу переписку", "/ai_reset"),
        ("Не забывай нашу переписку", "не забывай нашу переписку"),
    ],
)
def test_natural_controls(phrase, expected):
    assert control_intent(phrase) == expected


def test_errors_do_not_ask_nontechnical_user_to_configure_secrets():
    errors = [
        ProviderError("doctor --live HTTP 401"),
        TTSError("FISH_API_KEY missing"),
        MediaError("ENABLE_TRANSCRIPTION=true; pip install Whisper"),
    ]
    for error in errors:
        answer = simple_error(error)
        assert all(word not in answer for word in ("API_KEY", "doctor", "ENABLE_", "pip", "401"))
