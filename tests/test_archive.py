import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from tgaibot.archive import Archive, Archiver
from tgaibot.archive_web import create_app


def message(mid=1, text="Привет", outgoing=False, **kwargs):
    data = dict(
        id=mid,
        message=text,
        out=outgoing,
        sender_id=10,
        date=datetime.now(timezone.utc),
        file=None,
        media=None,
    )
    data.update(kwargs)
    return SimpleNamespace(**data)


@pytest.fixture
def archive(settings):
    value = Archive(settings)
    yield value
    value.close()


def test_dialogs_are_separate_and_outgoing_is_preserved(archive):
    archive.capture(10, "Анна", None, message())
    archive.capture(20, "Другая переписка", None, message(text="Секрет второго диалога"))
    archive.capture(10, "Анна", None, message(2, "Ответ", True))
    assert archive.stats()["dialogs"] == 2
    assert archive.stats()["messages"] == 3
    rows = archive.db.execute("SELECT * FROM messages WHERE dialog_id=10 ORDER BY id").fetchall()
    assert [r["text"] for r in rows] == ["Привет", "Ответ"]
    assert rows[1]["outgoing"] == 1


@pytest.mark.parametrize(
    "attr,kind",
    [
        ("photo", "photo"),
        ("video", "video"),
        ("video_note", "round_video"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("document", "file"),
    ],
)
def test_all_media_types_queued_without_ai_size_limit(archive, attr, kind):
    file = SimpleNamespace(
        size=250_000_000, name="original.bin", mime_type="application/octet-stream"
    )
    archive.capture(10, "Анна", None, message(file=file, **{attr: SimpleNamespace(id=33)}))
    row = archive.next_download()
    assert row["kind"] == kind and row["size"] == 250_000_000
    assert row["status"] == "pending"


def test_edits_keep_previous_text_and_reject_stale_history(archive):
    original = message()
    archive.capture(10, "Анна", None, original)
    edited = message(text="Исправлено", edit_date=datetime.now(timezone.utc) + timedelta(seconds=1))
    archive.capture(10, "Анна", None, edited)
    archive.capture(10, "Анна", None, original)
    assert archive.db.execute("SELECT text FROM messages").fetchone()[0] == "Исправлено"
    assert archive.db.execute("SELECT text FROM revisions").fetchone()[0] == "Привет"


def test_deletions_mark_but_do_not_erase_original(archive):
    archive.capture(10, "Анна", None, message())
    archive.deleted([1])
    row = archive.db.execute("SELECT * FROM messages").fetchone()
    assert row["deleted"] == 1 and row["text"] == "Привет"


def test_repeated_capture_does_not_requeue_saved_file(archive):
    value = message(
        file=SimpleNamespace(size=12, name="x.ogg", mime_type="audio/ogg"),
        document=SimpleNamespace(id=4),
        voice=True,
    )
    archive.capture(10, "Анна", None, value)
    archive.update_download(archive.next_download(), "ready", relative_path="10/1.ogg")
    archive.capture(10, "Анна", None, value)
    assert archive.next_download() is None


def test_viewer_open_does_not_reset_active_download(archive, settings):
    value = message(
        file=SimpleNamespace(size=12, name="x.ogg", mime_type="audio/ogg"),
        document=SimpleNamespace(id=4),
    )
    archive.capture(10, "Анна", None, value)
    archive.update_download(archive.next_download(), "downloading")
    viewer = Archive(settings, recover_downloads=False)
    viewer.close()
    assert archive.db.execute("SELECT status FROM messages").fetchone()[0] == "downloading"


async def test_full_history_sync_resumes_per_dialog(archive):
    async def dialogs():
        yield SimpleNamespace(
            id=10, is_user=True, entity=SimpleNamespace(username=None), name="Анна"
        )
        yield SimpleNamespace(id=20, is_user=False)

    seen = []

    def messages(entity, **kwargs):
        seen.append(kwargs)

        async def iterator():
            for mid in [2, 3]:
                if mid > kwargs["min_id"]:
                    yield message(mid)

        return iterator()

    archiver = Archiver(archive, SimpleNamespace(iter_dialogs=dialogs, iter_messages=messages))
    await archiver.sync()
    await archiver.sync()
    assert archive.stats()["messages"] == 2
    assert seen[1]["min_id"] == 3
    assert not archiver.syncing


async def test_archive_observation_includes_manual_outgoing(archive):
    archiver = Archiver(archive, None)
    event = SimpleNamespace(
        is_private=True,
        chat_id=10,
        message=message(outgoing=True),
        get_chat=AsyncMock(
            return_value=SimpleNamespace(first_name="Анна", last_name="", username="anna")
        ),
    )
    await archiver.observe(event)
    assert archive.db.execute("SELECT outgoing FROM messages").fetchone()[0] == 1


@pytest.fixture
async def web(settings, archive):
    configured = replace(settings, archive_password="private-test-password-123")
    app = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        yield client


async def login(web):
    return await web.post(
        "/api/login",
        json={"password": "private-test-password-123"},
        headers={"X-Archive-Request": "1"},
    )


async def test_viewer_requires_password_on_all_data_routes(web):
    for path in [
        "/api/dialogs",
        "/api/status",
        "/api/dialogs/10/messages",
        "/api/media/10/1",
        "/api/dialogs/10/messages/1/details",
    ]:
        assert (await web.get(path)).status_code == 401
    response = await login(web)
    assert response.status_code == 200
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "SameSite=strict" in response.headers["set-cookie"]
    )


async def test_viewer_dialog_isolation_and_russian_search(web, archive):
    archive.capture(10, "Анна", None, message(text="ПОЕЗДКА"))
    archive.capture(20, "Борис", None, message(text="Другое"))
    await login(web)
    assert len((await web.get("/api/dialogs", params={"q": "поездка"})).json()) == 1
    rows = (await web.get("/api/dialogs/10/messages")).json()
    assert [r["text"] for r in rows] == ["ПОЕЗДКА"]
    assert "metadata" not in rows[0]


async def test_pagination_does_not_mix_dialogs(web, archive):
    for mid in range(1, 75):
        archive.capture(10, "Анна", None, message(mid))
    await login(web)
    recent = (await web.get("/api/dialogs/10/messages")).json()
    old = (await web.get("/api/dialogs/10/messages", params={"before": recent[0]["id"]})).json()
    assert [r["id"] for r in old + recent] == list(range(1, 75))


async def test_media_ranges_and_html_download_safety(web, archive):
    value = message(
        file=SimpleNamespace(size=10, name="video.mp4", mime_type="video/mp4"),
        video=True,
        document=SimpleNamespace(id=9),
    )
    archive.capture(10, "Анна", None, value)
    path = archive.root / "10" / "video.mp4"
    path.parent.mkdir()
    path.write_bytes(b"0123456789")
    archive.update_download(archive.next_download(), "ready", relative_path="10/video.mp4")
    await login(web)
    response = await web.get("/api/media/10/1", headers={"Range": "bytes=2-5"})
    assert response.status_code == 206 and response.content == b"2345"
    archive.db.execute("UPDATE messages SET mime='text/html',filename='attack.html'")
    archive.db.commit()
    response = await web.get("/api/media/10/1")
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")


async def test_path_traversal_and_forged_session_rejected(web, archive):
    archive.capture(10, "Анна", None, message())
    archive.db.execute("UPDATE messages SET status='ready',relative_path='../history.sqlite3'")
    archive.db.commit()
    await login(web)
    assert (await web.get("/api/media/10/1")).status_code == 404
    web.cookies.clear()
    web.cookies.set("archive_session", "99999999999.nonce.forged")
    assert (await web.get("/api/dialogs")).status_code == 401


async def test_login_rate_limit_and_csrf(web):
    assert (await web.post("/api/login", json={"password": "wrong"})).status_code == 403
    for _ in range(5):
        assert (
            await web.post(
                "/api/login", json={"password": "wrong"}, headers={"X-Archive-Request": "1"}
            )
        ).status_code == 401
    assert (await login(web)).status_code == 429


async def test_archive_download_saves_exact_original_and_survives_memory_reset(archive, history):
    payload = b"original-media-bytes"
    value = message(
        file=SimpleNamespace(size=len(payload), name="note.ogg", mime_type="audio/ogg", ext=".ogg"),
        voice=True,
        document=SimpleNamespace(id=123),
        media=object(),
    )
    archive.capture(10, "Анна", None, value)

    async def stream(*args, **kwargs):
        yield payload[:5]
        yield payload[5:]

    client = SimpleNamespace(get_messages=AsyncMock(return_value=value), iter_download=stream)
    archiver = Archiver(archive, client)
    completed = asyncio.Event()
    update = archive.update_download

    def observed_update(row, status, **kwargs):
        update(row, status, **kwargs)
        if status == "ready":
            completed.set()

    archive.update_download = observed_update
    archiver.download_task = asyncio.create_task(archiver.downloads())
    await asyncio.wait_for(completed.wait(), 2)
    await archiver.close()
    history.reset(10)
    row = archive.db.execute("SELECT * FROM messages").fetchone()
    assert row["status"] == "ready"
    assert (archive.root / row["relative_path"]).read_bytes() == payload


async def test_partial_download_is_not_marked_ready(archive):
    value = message(
        file=SimpleNamespace(size=10, name="x.mp4", mime_type="video/mp4", ext=".mp4"),
        video=True,
        document=SimpleNamespace(id=123),
        media=object(),
    )
    archive.capture(10, "Анна", None, value)

    async def stream(*args, **kwargs):
        yield b"short"

    archiver = Archiver(
        archive, SimpleNamespace(get_messages=AsyncMock(return_value=value), iter_download=stream)
    )
    completed = asyncio.Event()
    update = archive.update_download

    def observed_update(row, status, **kwargs):
        update(row, status, **kwargs)
        if status == "failed":
            completed.set()

    archive.update_download = observed_update
    archiver.download_task = asyncio.create_task(archiver.downloads())
    await asyncio.wait_for(completed.wait(), 2)
    await archiver.close()
    row = archive.db.execute("SELECT * FROM messages").fetchone()
    assert row["status"] == "failed" and row["attempts"] == 1
    assert not list(archive.root.rglob("*.part"))
