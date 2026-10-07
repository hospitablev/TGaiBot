import io
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image

from tgaibot import web_photos as photos
from tgaibot.media import MediaError
from tgaibot.search import Search


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(output, "JPEG")
    return output.getvalue()


def row(number=1):
    return {
        "title": "Seafood doenjang jjigae",
        "url": f"https://example.com/recipe/{number}",
        "image": f"https://images.example.com/{number}.jpg",
        "thumbnail": f"https://thumbs.example.com/{number}.jpg",
    }


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://localhost/a",
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://[::1]/a",
        "http://169.254.169.254/",
        "https://name:secret@example.com/x",
        "https://example.com:8080/",
        "https://example.com/\nx",
        "http://[::ffff:127.0.0.1]/",
    ],
)
def test_reject_nonpublic_urls(url):
    with pytest.raises(ValueError):
        photos.public_url(url)


async def test_dns_mixed_public_private_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(ValueError):
        await photos.public_ip("photos.example.com", 443)


def transport(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(photos, "public_ip", AsyncMock(return_value="8.8.8.8"))
    monkeypatch.setattr(
        photos.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )


async def test_connection_pinned_with_original_host_tls_name_and_no_auth(monkeypatch):
    def handle(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["Host"] == "images.example.com"
        assert request.extensions["sni_hostname"] == "images.example.com"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=jpeg())

    transport(monkeypatch, handle)
    assert await photos.download_photo("https://images.example.com/a.jpg") == jpeg()


async def test_private_redirect_is_blocked_before_request(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    transport(monkeypatch, handle)
    with pytest.raises(ValueError):
        await photos.download_photo("https://images.example.com/a.jpg")
    assert len(requests) == 1


@pytest.mark.parametrize("declared", [True, False])
async def test_download_byte_limit(monkeypatch, declared):
    monkeypatch.setattr(photos, "MAX_BYTES", 10)

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 6
            yield b"x" * 5

    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-length": "11"} if declared else {}, stream=Chunks()
        ),
    )
    with pytest.raises(ValueError):
        await photos.download_photo("https://images.example.com/a.jpg")


async def test_compressed_response_rejected_before_decoding(monkeypatch):
    class Bomb(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("Must not decode compressed content")
            yield b""

    transport(
        monkeypatch,
        lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Bomb()),
    )
    with pytest.raises(ValueError, match="сжатие"):
        await photos.download_photo("https://images.example.com/a.jpg")


def test_invalid_content_not_saved_as_photo(tmp_path):
    with pytest.raises(MediaError):
        photos.prepare_photo(b"<html>not a photo</html>", tmp_path)
    assert not list(tmp_path.iterdir())


async def test_reject_wrong_photo_then_verify_next_and_preserve_source(tmp_path, monkeypatch):
    download = AsyncMock(return_value=jpeg())
    monkeypatch.setattr(photos, "download_photo", download)
    provider = SimpleNamespace(
        answer=AsyncMock(side_effect=['{"match": false}', '{"match": true}'])
    )
    search = SimpleNamespace(images=AsyncMock(return_value=[row(1), row(2)]))
    result = await photos.find_photo(search, provider, "해물된장찌개", tmp_path)
    assert result["source"] == row(2)["url"] and Path(result["path"]).exists()
    assert len(list(tmp_path.glob("*.jpg"))) == 1
    assert download.await_count == 2
    assert "해물된장찌개" in str(provider.answer.call_args)


async def test_unreachable_original_uses_verified_thumbnail(tmp_path, monkeypatch):
    download = AsyncMock(side_effect=[TimeoutError(), jpeg()])
    monkeypatch.setattr(photos, "download_photo", download)
    search = SimpleNamespace(images=AsyncMock(return_value=[row()]))
    provider = SimpleNamespace(answer=AsyncMock(return_value='{"match": true}'))
    await photos.find_photo(search, provider, "soup", tmp_path)
    assert download.await_args.args[0] == row()["thumbnail"]
    provider.answer.assert_awaited_once()


async def test_unverified_photo_never_returned(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "download_photo", AsyncMock(return_value=jpeg()))
    with pytest.raises(ValueError):
        await photos.find_photo(
            SimpleNamespace(images=AsyncMock(return_value=[row()])),
            SimpleNamespace(answer=AsyncMock(return_value="probably yes")),
            "soup",
            tmp_path,
        )
    assert not list(tmp_path.glob("*.jpg"))


async def test_send_failure_does_not_try_another_photo_and_restart_reuses_selection(
    tmp_path, monkeypatch
):
    from tgaibot.turns import CURRENT_TURN

    photo = {"path": str(tmp_path / "soup.jpg"), "source": row()["url"], "title": "Soup"}
    Path(photo["path"]).write_bytes(jpeg())
    find = AsyncMock(return_value=photo)
    monkeypatch.setattr(photos, "find_photo", find)
    agent = SimpleNamespace(
        search=None,
        provider=SimpleNamespace(),
        client=SimpleNamespace(send_file=AsyncMock(side_effect=OSError("connection lost"))),
    )
    turn = SimpleNamespace(data={}, scope="tool:photo", save=Mock())
    token = CURRENT_TURN.set(turn)
    try:
        with pytest.raises(OSError):
            await photos.send_web_photo(agent, "soup", tmp_path, 45, 123, lambda: True)
        assert turn.data["web_photos"][turn.scope] == photo
        # Simulate reloading the durable state; network delivery remains Turn.rpc's responsibility.
        turn.data = json.loads(json.dumps(turn.data))
        agent.client.send_file.side_effect = None
        result = await photos.send_web_photo(agent, "soup", tmp_path, 45, 123, lambda: True)
        find.assert_awaited_once()
        assert result["sent"] and result["source"] == row()["url"]
        call = agent.client.send_file.await_args
        assert call.args == (45, photo["path"])
        assert call.kwargs["reply_to"] == 123 and call.kwargs["force_document"] is False
        assert row()["url"] in call.kwargs["caption"] and call.kwargs["parse_mode"] is None
    finally:
        CURRENT_TURN.reset(token)


async def test_reset_during_search_prevents_send(tmp_path, monkeypatch):
    photo = {"path": str(tmp_path / "soup.jpg"), "source": row()["url"], "title": "Soup"}
    monkeypatch.setattr(photos, "find_photo", AsyncMock(return_value=photo))
    agent = SimpleNamespace(
        search=None, provider=None, client=SimpleNamespace(send_file=AsyncMock())
    )
    with pytest.raises(ValueError):
        await photos.send_web_photo(agent, "soup", tmp_path, 45, 123, lambda: False)
    agent.client.send_file.assert_not_awaited()


async def test_image_search_uses_original_query_and_bounded_results(settings, monkeypatch):
    engine = Mock()
    engine.images.return_value = [row(i) for i in range(20)]
    monkeypatch.setattr("tgaibot.search.DDGS", Mock(return_value=engine))
    search = Search(settings)
    try:
        results = await search.images("해물된장찌개")
        assert len(results) == 6 and results[0]["image"] == row(0)["image"]
        assert engine.images.call_args.args == ("해물된장찌개",)
    finally:
        await search.close()
