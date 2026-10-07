"""Public search results; provider credentials never reach search services."""

import asyncio
import math
import time
from urllib.parse import urlparse

import httpx
from ddgs import DDGS

from .artifacts import bounded_text


class Search:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            headers={"User-Agent": "TGaiBot/0.1 (personal Telegram assistant)"},
        )
        self.map_lock = asyncio.Lock()
        self.last_map_request = 0.0
        self.map_cache = {}

    async def close(self):
        await self.client.aclose()

    async def web(self, query):
        bounded_text(query, 400)
        if self.settings.tavily_key:
            response = await self.client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": "Bearer " + self.settings.tavily_key},
                json={"query": query, "max_results": 5, "include_raw_content": False},
            )
            response.raise_for_status()
            rows = response.json()["results"]
        else:
            rows = await asyncio.to_thread(
                lambda: DDGS(timeout=12).text(
                    query, max_results=5, backend="bing,duckduckgo", safesearch="moderate"
                )
            )
        results = []
        for row in rows[:5]:
            url = row.get("url") or row.get("href") or ""
            parsed = urlparse(url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
                continue
            results.append(
                {
                    "title": str(row.get("title", ""))[:300],
                    "url": url[:2000],
                    "snippet": str(row.get("content") or row.get("body") or "")[:2000],
                }
            )
        return {
            "results": results,
            "note": "Это фрагменты поисковой выдачи, не полные страницы. Проверяй соответствие запросу и приводи ссылки.",
        }

    async def places(self, query):
        bounded_text(query, 300)
        async with self.map_lock:
            cached = self.map_cache.get(query)
            if cached and time.monotonic() - cached[0] < 86400:
                return cached[1]
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self.last_map_request)))
            self.last_map_request = time.monotonic()
            response = await self.client.get(
                "https://photon.komoot.io/api/", params={"q": query, "limit": 5}
            )
            response.raise_for_status()
            places = []
            for row in response.json()["features"][:5]:
                lon, lat = map(float, row["geometry"]["coordinates"][:2])
                if not (
                    math.isfinite(lat)
                    and math.isfinite(lon)
                    and -90 <= lat <= 90
                    and -180 <= lon <= 180
                ):
                    continue
                props = row["properties"]
                label = ", ".join(
                    dict.fromkeys(
                        str(props[k])
                        for k in ("name", "street", "housenumber", "city", "state", "country")
                        if props.get(k)
                    )
                )
                places.append(
                    {
                        "name": label[:700],
                        "kind": str(props.get("osm_value", ""))[:100],
                        "latitude": lat,
                        "longitude": lon,
                        "url": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}",
                    }
                )
            result = {
                "places": places,
                "attribution": "© OpenStreetMap contributors",
                "note": "Совпадения адресов, не гарантия актуальности организаций или часов работы.",
            }
            if len(self.map_cache) >= 200:
                self.map_cache.pop(next(iter(self.map_cache)))
            self.map_cache[query] = (time.monotonic(), result)
            return result
