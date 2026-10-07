from dataclasses import replace

import httpx
import pytest

from tgaibot.search import Search


async def test_map_cache_and_validated_coordinates(settings):
    requests = []

    def handle(request):
        requests.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "features": [
                    {"geometry": {"coordinates": [76.9, 43.2]}, "properties": {"name": "Алматы"}},
                    {"geometry": {"coordinates": [0, "NaN"]}, "properties": {"name": "bad"}},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        search = Search(settings, client)
        result = await search.places("Алматы")
        assert len(result["places"]) == 1
        assert await search.places("Алматы") == result
        assert len(requests) == 1


async def test_tavily_key_scoped_and_unsafe_links_filtered(settings):
    settings = replace(settings, tavily_key="search-test-key")

    def handle(request):
        assert request.url.host == "api.tavily.com"
        assert request.headers["Authorization"] == "Bearer search-test-key"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "ok", "url": "https://example.com", "content": "snippet"},
                    {"title": "bad", "url": "javascript:alert(1)"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await Search(settings, client).web("test")
        assert len(result["results"]) == 1


async def test_search_never_follows_redirect(settings):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "https://example.com"})
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await Search(settings, client).places("test")
