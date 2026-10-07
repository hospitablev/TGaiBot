"""Explicit image requests through the user's A6 provider, never another image service."""

import asyncio
import base64
import binascii
import io
import re

import httpx
from PIL import Image, UnidentifiedImageError

from .provider import ProviderError

IMAGE_MODEL = "gpt-image-2.5-flare"


def image_prompt(text):
    text = text.strip()
    if re.match(r"^/image(?:\s|$)", text, flags=re.I):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""
    match = re.match(
        r"^(?:пожалуйста[, ]+)?(?:нарисуй|сгенерируй\s+(?:картинку|изображение)|"
        r"создай\s+(?:картинку|изображение)|generate\s+(?:an?\s+)?image|draw)\s+(.+)$",
        text,
        flags=re.I | re.S,
    )
    return match.group(1).strip() if match else None


class ImageGenerator:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=15), follow_redirects=False
        )

    async def close(self):
        await self.client.aclose()

    async def generate(self, prompt, target):
        if not prompt or len(prompt) > 4000:
            raise ProviderError("После /image укажите описание длиной от 1 до 4000 символов.")
        try:
            async with asyncio.timeout(180):
                async with self.client.stream(
                    "POST",
                    self.settings.api_base + "/images/generations",
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                    json={
                        "model": IMAGE_MODEL,
                        "prompt": prompt,
                        "n": 1,
                        "size": "1024x1024",
                        "quality": "low",
                        "output_format": "png",
                    },
                ) as response:
                    if response.status_code != 200:
                        raise ProviderError(
                            f"Генерация {IMAGE_MODEL} отклонена провайдером "
                            f"(HTTP {response.status_code}). Модель не заменялась."
                        )
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 28_000_000:
                            raise ProviderError("Ответ генератора превышает лимит 28 МБ.")
            import json

            data = json.loads(raw)
            if data.get("model") and data["model"] != IMAGE_MODEL:
                raise ProviderError(
                    "Провайдер вернул другой ID модели изображения; результат отклонён."
                )
            encoded = data["data"][0].get("b64_json")
            if not encoded:
                raise ProviderError(
                    "Провайдер не вернул изображение в b64_json. Ссылки не скачиваются автоматически."
                )
            image = base64.b64decode(encoded, validate=True)
            with Image.open(io.BytesIO(image)) as decoded:
                if decoded.format != "PNG" or decoded.width * decoded.height > 20_000_000:
                    raise ProviderError("Генератор вернул неподдерживаемое изображение.")
                decoded.verify()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(image)
            return target
        except (httpx.HTTPError, TimeoutError):
            raise ProviderError("Генератор изображений недоступен или не ответил за 180 секунд.")
        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            binascii.Error,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            OSError,
        ):
            raise ProviderError("Не удалось прочитать изображение в ответе провайдера.")
