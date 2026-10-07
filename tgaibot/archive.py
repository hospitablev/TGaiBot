"""Owner's conversation archive, independent of AI quotas and memory."""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone

from telethon import errors

LOG = logging.getLogger(__name__)


def json_default(value):
    if isinstance(value, bytes):
        return {"bytes_length": len(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class Archive:
    def __init__(self, settings, *, recover_downloads=True):
        self.settings = settings
        self.root = settings.data_dir / "archive-media"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / "archive.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS dialogs (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
                dialog_id INTEGER NOT NULL, id INTEGER NOT NULL, date REAL NOT NULL,
                outgoing INTEGER NOT NULL, sender_id INTEGER, text TEXT NOT NULL,
                kind TEXT NOT NULL, filename TEXT, mime TEXT, size INTEGER,
                reply_to INTEGER, grouped_id TEXT, edited REAL, deleted INTEGER DEFAULT 0,
                metadata TEXT NOT NULL, media_key TEXT, relative_path TEXT,
                status TEXT NOT NULL DEFAULT 'none', error TEXT, attempts INTEGER DEFAULT 0,
                retry_at REAL DEFAULT 0, PRIMARY KEY(dialog_id,id));
            CREATE INDEX IF NOT EXISTS message_dates ON messages(dialog_id,date,id);
            CREATE INDEX IF NOT EXISTS message_jobs ON messages(status,retry_at);
            CREATE TABLE IF NOT EXISTS revisions (
                dialog_id INTEGER, message_id INTEGER, observed REAL, text TEXT, metadata TEXT);
            CREATE TABLE IF NOT EXISTS deletions (
                dialog_id INTEGER, message_id INTEGER, PRIMARY KEY(dialog_id,message_id));
            CREATE TABLE IF NOT EXISTS sync_state (
                dialog_id INTEGER PRIMARY KEY, high_water INTEGER DEFAULT 0);
        """)
        # Interrupted downloads are safely retried after restart.
        if recover_downloads:
            self.db.execute("UPDATE messages SET status='pending' WHERE status='downloading'")
        self.db.commit()

    def close(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()

    def capture(self, chat_id, title, username, message):
        if getattr(self, "call_journal", None):
            self.call_journal.service(chat_id, message)
        file = getattr(message, "file", None)
        kind = next(
            (
                name
                for attr, name in [
                    ("video_note", "round_video"),
                    ("voice", "voice"),
                    ("photo", "photo"),
                    ("video", "video"),
                    ("audio", "audio"),
                    ("sticker", "sticker"),
                    ("document", "file"),
                    ("geo", "location"),
                    ("contact", "contact"),
                    ("poll", "poll"),
                ]
                if getattr(message, attr, None)
            ),
            "service" if getattr(message, "action", None) else "text",
        )
        date = getattr(message, "date", None) or datetime.now(timezone.utc)
        edit = getattr(message, "edit_date", None)
        media = getattr(message, "document", None) or getattr(message, "photo", None)
        media_key = str(getattr(media, "id", "")) if file else None
        if file and not media_key:
            media_key = f"{getattr(file, 'size', 0)}:{getattr(file, 'name', '')}"
        raw = message.to_dict() if hasattr(message, "to_dict") else {"text": message.message}
        metadata = json.dumps(raw, ensure_ascii=False, default=json_default)
        text = getattr(message, "message", "") or ""
        old = self.db.execute(
            "SELECT * FROM messages WHERE dialog_id=? AND id=?", (chat_id, message.id)
        ).fetchone()
        # A concurrently received newer edit must not be overwritten by an older history page.
        if old and old["edited"] and (not edit or edit.timestamp() < old["edited"]):
            return
        with self.db:
            self.db.execute(
                """INSERT INTO dialogs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,username=excluded.username,updated=MAX(dialogs.updated,excluded.updated)""",
                (chat_id, title, username, date.timestamp()),
            )
            if old and (old["text"] != text or old["media_key"] != media_key):
                self.db.execute(
                    "INSERT INTO revisions VALUES(?,?,?,?,?)",
                    (chat_id, message.id, time.time(), old["text"], old["metadata"]),
                )
            same_media = old and old["media_key"] == media_key
            status = old["status"] if same_media else "pending" if file else "none"
            relative_path = old["relative_path"] if same_media else None
            values = (
                chat_id,
                message.id,
                date.timestamp(),
                int(bool(getattr(message, "out", False))),
                getattr(message, "sender_id", None),
                text,
                kind,
                getattr(file, "name", None),
                getattr(file, "mime_type", None),
                getattr(file, "size", None),
                getattr(message, "reply_to_msg_id", None),
                str(getattr(message, "grouped_id", "") or ""),
                edit.timestamp() if edit else None,
                int(
                    bool(old and old["deleted"])
                    or self.db.execute(
                        "SELECT 1 FROM deletions WHERE message_id=? AND dialog_id IN (0,?)",
                        (message.id, chat_id),
                    ).fetchone()
                    is not None
                ),
                metadata,
                media_key,
                relative_path,
                status,
                old["error"] if same_media else None,
                old["attempts"] if same_media else 0,
                old["retry_at"] if same_media else 0,
            )
            self.db.execute(
                "INSERT OR REPLACE INTO messages VALUES(" + ",".join("?" for _ in values) + ")",
                values,
            )

    def deleted(self, ids, chat_id=None):
        with self.db:
            for mid in ids:
                self.db.execute("INSERT OR IGNORE INTO deletions VALUES(?,?)", (chat_id or 0, mid))
                if chat_id is None:
                    self.db.execute("UPDATE messages SET deleted=1 WHERE id=?", (mid,))
                else:
                    self.db.execute(
                        "UPDATE messages SET deleted=1 WHERE dialog_id=? AND id=?", (chat_id, mid)
                    )

    def update_download(self, row, status, *, relative_path=None, error=None):
        with self.db:
            self.db.execute(
                """UPDATE messages SET status=?, relative_path=COALESCE(?,relative_path),
                error=?, attempts=attempts+?,retry_at=? WHERE dialog_id=? AND id=? AND media_key=?""",
                (
                    status,
                    relative_path,
                    error,
                    int(status == "failed"),
                    time.time() + 300,
                    row["dialog_id"],
                    row["id"],
                    row["media_key"],
                ),
            )

    def next_download(self):
        return self.db.execute(
            """SELECT * FROM messages WHERE status='pending' OR
            (status='failed' AND attempts<5 AND retry_at<?) ORDER BY date DESC LIMIT 1""",
            (time.time(),),
        ).fetchone()

    def high_water(self, chat_id):
        row = self.db.execute(
            "SELECT high_water FROM sync_state WHERE dialog_id=?", (chat_id,)
        ).fetchone()
        return row[0] if row else 0

    def advance(self, chat_id, mid):
        with self.db:
            self.db.execute(
                "INSERT INTO sync_state VALUES(?,?) ON CONFLICT(dialog_id) DO UPDATE SET high_water=MAX(high_water,excluded.high_water)",
                (chat_id, mid),
            )

    def stats(self):
        return {
            "dialogs": self.db.execute("SELECT count(*) FROM dialogs").fetchone()[0],
            "messages": self.db.execute("SELECT count(*) FROM messages").fetchone()[0],
            "media": dict(self.db.execute("SELECT status,count(*) FROM messages GROUP BY status")),
        }


class Archiver:
    def __init__(self, archive, client):
        self.archive, self.client = archive, client
        self.wakeup = asyncio.Event()
        self.syncing = False
        self.sync_error = None
        self.sync_task = None
        self.download_task = None

    async def observe(self, event):
        if not event.is_private or not event.chat_id:
            return
        try:
            chat = await event.get_chat()
            title = (
                " ".join(
                    filter(
                        None, [getattr(chat, "first_name", None), getattr(chat, "last_name", None)]
                    )
                )
                or getattr(chat, "username", None)
                or str(event.chat_id)
            )
            self.archive.capture(
                event.chat_id, title, getattr(chat, "username", None), event.message
            )
            self.wakeup.set()
        except Exception as exc:
            LOG.error("Не удалось сохранить сообщение в архив (%s)", type(exc).__name__)

    def start(self):
        self.download_task = asyncio.create_task(self.downloads())
        self.start_sync()

    def start_sync(self):
        if self.sync_task and not self.sync_task.done():
            return False
        self.sync_task = asyncio.create_task(self.sync())
        return True

    async def sync(self):
        self.syncing, self.sync_error = True, None
        try:
            async for dialog in self.client.iter_dialogs():
                if not dialog.is_user:
                    continue
                minimum = self.archive.high_water(dialog.id)
                async for message in self.client.iter_messages(
                    dialog.entity, min_id=minimum, reverse=True, wait_time=1
                ):
                    self.archive.capture(
                        dialog.id, dialog.name, getattr(dialog.entity, "username", None), message
                    )
                    self.archive.advance(dialog.id, message.id)
                    self.wakeup.set()
                    await asyncio.sleep(0)
        except errors.FloodWaitError as exc:
            self.sync_error = (
                f"Telegram просит подождать {exc.seconds} с. Импорт можно продолжить позже."
            )
        except Exception as exc:
            self.sync_error = (
                "Историю не удалось загрузить полностью. Уже сохранённое осталось в архиве."
            )
            LOG.warning("Импорт истории остановлен (%s)", type(exc).__name__)
        finally:
            self.syncing = False

    async def downloads(self):
        while True:
            row = self.archive.next_download()
            if not row:
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), 30)
                except TimeoutError:
                    pass
                continue
            path = None
            try:
                self.archive.update_download(row, "downloading")
                message = await self.client.get_messages(row["dialog_id"], ids=row["id"])
                if not message or not message.file:
                    raise ValueError("Вложение больше недоступно в Telegram.")
                media = getattr(message, "document", None) or getattr(message, "photo", None)
                media_key = str(getattr(media, "id", ""))
                if media_key and media_key != row["media_key"]:
                    raise ValueError("Вложение было заменено до сохранения оригинала.")
                suffix = getattr(message.file, "ext", "") or ""
                if not re.fullmatch(r"\.[a-zA-Z0-9]{1,10}", suffix):
                    suffix = ".bin"
                fingerprint = hashlib.sha256(row["media_key"].encode()).hexdigest()[:16]
                relative = f"{row['dialog_id']}/{row['id']}-{fingerprint}{suffix}"
                target = self.archive.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                path = target.with_suffix(target.suffix + ".part")
                expected = getattr(message.file, "size", None) or 0
                if shutil.disk_usage(self.archive.root).free < expected + 100_000_000:
                    raise ValueError("Не хватает места в постоянном хранилище для оригинала.")
                size = 0
                async with asyncio.timeout(3600):
                    with path.open("wb") as handle:
                        async for chunk in self.client.iter_download(
                            message.media, request_size=512 * 1024
                        ):
                            if shutil.disk_usage(self.archive.root).free < len(chunk) + 50_000_000:
                                raise ValueError("Хранилище заполнено; оригинал пока не сохранён.")
                            handle.write(chunk)
                            size += len(chunk)
                if not size or (expected and size != expected):
                    raise ValueError("Вложение скачано не полностью; загрузка будет повторена.")
                path.replace(target)
                self.archive.update_download(row, "ready", relative_path=relative)
            except asyncio.CancelledError:
                self.archive.update_download(row, "pending")
                raise
            except Exception as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Загрузка не завершена. Попробуем ещё раз."
                )
                self.archive.update_download(row, "failed", error=reason)
                if isinstance(exc, errors.FloodWaitError):
                    await asyncio.sleep(exc.seconds)
            finally:
                if path:
                    path.unlink(missing_ok=True)

    async def close(self):
        tasks = [task for task in (self.download_task, self.sync_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
