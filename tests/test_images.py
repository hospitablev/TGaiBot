import base64
import io
import json

import httpx
import pytest
from PIL import Image

from tgaibot.images import IMAGE_MODEL, ImageGenerator, image_prompt
from tgaibot.provider import ProviderError


def test_explicit_image_intents_only():
    assert image_prompt("/image красный кораблик") == "красный кораблик"
    assert image_prompt("Нарисуй синий круг") == "синий круг"
    assert image_prompt("расскажи, как нарисовать круг") is None
    assert image_prompt("не рисуй картинку") is None


async def test_image_api_exact_model_and_local_result(settings, tmp_path):
    data = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(data, format="PNG")

    def route(request):
        assert str(request.url).endswith("/v1/images/generations")
        assert json.loads(request.content)["model"] == IMAGE_MODEL
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(data.getvalue()).decode()}]}
        )

    generator = ImageGenerator(settings, httpx.AsyncClient(transport=httpx.MockTransport(route)))
    result = await generator.generate("red", tmp_path / "image.png")
    assert result.read_bytes() == data.getvalue()
    await generator.close()


async def test_image_model_not_silently_replaced(settings, tmp_path):
    generator = ImageGenerator(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"model": "another-model"})
            )
        ),
    )
    with pytest.raises(ProviderError, match="другой ID"):
        await generator.generate("test", tmp_path / "image.png")
    await generator.close()
