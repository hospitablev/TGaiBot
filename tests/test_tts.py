import json
from dataclasses import replace

import httpx
import pytest

from tgaibot.tts import FishTTS, TTSError, speech_text


async def test_exact_fish_model_voice_and_credential_separation(settings, tmp_path):
    settings = replace(
        settings,
        fish_key="fish-test-placeholder",
        fish_model="s2.1-pro-free",
        fish_voice="chosen-voice",
    )

    def route(request):
        assert str(request.url) == "https://api.fish.audio/v1/tts"
        assert request.headers["model"] == "s2.1-pro-free"
        assert request.headers["authorization"] == "Bearer fish-test-placeholder"
        assert settings.api_key not in str(request.headers)
        body = json.loads(request.content)
        assert body["reference_id"] == "chosen-voice" and body["format"] == "mp3"
        return httpx.Response(200, content=b"ID3" + b"test" * 100)

    tts = FishTTS(settings, httpx.AsyncClient(transport=httpx.MockTransport(route)))
    path = await tts.synthesize("Проверка", tmp_path / "test.mp3")
    assert path.read_bytes().startswith(b"ID3")
    await tts.close()


@pytest.mark.parametrize("status", [302, 401, 402, 429, 500, 503])
async def test_tts_error_safe(settings, tmp_path, status):
    tts = FishTTS(
        replace(settings, fish_key="fake"),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, text="PRIVATE_ERROR")
            )
        ),
    )
    with pytest.raises(TTSError) as exc:
        await tts.synthesize("Hello", tmp_path / "test.mp3")
    assert "PRIVATE_ERROR" not in str(exc.value)
    assert not (tmp_path / "test.mp3").exists()
    await tts.close()


async def test_missing_key_no_request(settings, tmp_path):
    tts = FishTTS(settings)
    with pytest.raises(TTSError, match="FISH_API_KEY"):
        await tts.synthesize("Hello", tmp_path / "test.mp3")
    await tts.close()


def test_speech_disclosure_code_and_truncation():
    text, truncated = speech_text("Пример ```python\nsecret_code()\n``` " + "слово " * 1000)
    assert text.startswith("Пример")
    assert "secret_code" not in text and truncated
    assert "Продолжение" in text
