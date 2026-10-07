"""Fish Audio TTS. The Claude key is never used by this client."""

import asyncio

import httpx

from .formatting import spoken_markdown
from .media import run_tool


class TTSError(Exception):
    pass


def speech_text(text, limit=1500):
    text = spoken_markdown(text)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit].rsplit(" ", 1)[0] + ". Продолжение — в текстовом ответе."
    return "Отвечает искусственный интеллект. " + text, truncated


class FishTTS:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(45, connect=10), follow_redirects=False
        )

    async def close(self):
        await self.client.aclose()

    async def synthesize(self, text, target):
        if not self.settings.fish_key:
            raise TTSError("Озвучка пока не настроена: нужен отдельный FISH_API_KEY.")
        if not text.strip() or len(text) > 2000:
            raise TTSError("Для одной озвучки нужен текст от 1 до 2000 символов.")
        body = {
            "text": text,
            "format": "mp3",
            "mp3_bitrate": 128,
            "latency": "normal",
            "prosody": {"speed": self.settings.fish_speed},
        }
        if self.settings.fish_voice:
            body["reference_id"] = self.settings.fish_voice
        try:
            async with asyncio.timeout(60):
                async with self.client.stream(
                    "POST",
                    "https://api.fish.audio/v1/tts",
                    json=body,
                    headers={
                        "Authorization": "Bearer " + self.settings.fish_key,
                        "model": self.settings.fish_model,
                    },
                ) as response:
                    if response.status_code != 200:
                        raise TTSError(
                            f"Fish Audio временно недоступен (HTTP {response.status_code}). "
                            "Ответ сохранён текстом."
                        )
                    size, prefix = 0, bytearray()
                    with target.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > 10_000_000:
                                raise TTSError(
                                    "Аудио превысило лимит 10 МБ. Ответ сохранён текстом."
                                )
                            if len(prefix) < 4:
                                prefix.extend(chunk[: 4 - len(prefix)])
                            output.write(chunk)
                    if not (
                        prefix.startswith(b"ID3")
                        or len(prefix) >= 2
                        and prefix[0] == 255
                        and prefix[1] & 224 == 224
                    ):
                        raise TTSError(
                            "Fish Audio вернул некорректный MP3. Ответ сохранён текстом."
                        )
        except (httpx.HTTPError, TimeoutError):
            target.unlink(missing_ok=True)
            raise TTSError(
                "Не удалось получить озвучку за отведённое время. Ответ сохранён текстом."
            )
        except TTSError:
            target.unlink(missing_ok=True)
            raise
        return target

    async def voice_note(self, text, directory):
        spoken, truncated = speech_text(text)
        mp3 = await self.synthesize(spoken, directory / "answer.mp3")
        ogg = directory / "answer.ogg"
        await asyncio.to_thread(
            run_tool,
            [
                self.settings.ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(mp3),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                str(ogg),
            ],
        )
        return ogg, truncated
