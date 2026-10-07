import httpx
import pytest

from tgaibot.provider import Provider, ProviderError


async def test_multimodal_request_and_answer(settings):
    import json

    seen = []

    def route(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "model": settings.model,
                "choices": [{"message": {"content": "Ответ"}, "finish_reason": "stop"}],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    provider = Provider(settings, client)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Photo"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA=="}},
            ],
        }
    ]
    assert await provider.answer(messages) == "Ответ"
    assert seen[0]["model"] == "claude-sonnet-5-5"
    assert seen[0]["messages"][1:] == messages
    assert "temperature" not in seen[0]
    await provider.close()


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 422, 429, 500, 503])
async def test_error_does_not_expose_body_or_secrets(settings, status):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="PRIVATE_RESPONSE_BODY")
        )
    )
    provider = Provider(settings, client)
    with pytest.raises(ProviderError) as result:
        await provider.answer([])
    assert "PRIVATE_RESPONSE_BODY" not in str(result.value)
    assert settings.api_key not in str(result.value)
    await provider.close()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"model": "other-model"},
        {"model": "claude-sonnet-5-5", "choices": []},
        {"model": "claude-sonnet-5-5", "choices": [{"message": {"content": ""}}]},
    ],
)
async def test_invalid_or_substituted_model_fails(settings, body):
    provider = Provider(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        ),
    )
    with pytest.raises(ProviderError):
        await provider.answer([])
    await provider.close()


async def test_timeout_is_safe(settings):
    def route(request):
        raise httpx.ReadTimeout("PRIVATE", request=request)

    provider = Provider(settings, httpx.AsyncClient(transport=httpx.MockTransport(route)))
    with pytest.raises(ProviderError, match="120 секунд"):
        await provider.answer([])
    await provider.close()


async def test_tool_response_preserves_call_for_execution(settings):
    call = {
        "id": "call1",
        "type": "function",
        "function": {"name": "search_web", "arguments": '{"query":"test"}'},
    }
    provider = Provider(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "model": settings.model,
                        "choices": [
                            {
                                "message": {"content": None, "tool_calls": [call]},
                                "finish_reason": "tool_calls",
                            }
                        ],
                    },
                )
            )
        ),
    )
    assert (await provider.step([], []))["tool_calls"] == [call]
    await provider.close()


async def test_truncated_tool_arguments_are_never_executed(settings):
    provider = Provider(
        settings,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "model": settings.model,
                        "choices": [{"message": {"tool_calls": [{}]}, "finish_reason": "length"}],
                    },
                )
            )
        ),
    )
    with pytest.raises(ProviderError, match="обрезан"):
        await provider.step([], [])
    await provider.close()
