"""Password-protected, read-only conversation viewer with media streaming."""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


def create_app(settings, archiver=None):
    if len(settings.archive_password) < 16:
        raise RuntimeError("Для просмотра архива задайте ARCHIVE_PASSWORD (от 16 символов).")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    signing_key = secrets.token_bytes(32)
    failures = {}

    @contextmanager
    def database():
        db = sqlite3.connect(settings.data_dir / "archive.sqlite3")
        db.row_factory = sqlite3.Row
        db.create_function("casefold", 1, lambda text: (text or "").casefold())
        try:
            yield db
        finally:
            db.close()

    def valid_cookie(value):
        try:
            if not value or len(value) > 200:
                return False
            expires, nonce, signature = value.split(".")
            expected = hmac.new(
                signing_key, f"{expires}.{nonce}".encode(), hashlib.sha256
            ).hexdigest()
            return int(expires) > time.time() and hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    @app.middleware("http")
    async def protect(request, call_next):
        if request.url.path.startswith("/api/") and request.url.path != "/api/login":
            if not valid_cookie(request.cookies.get("archive_session")):
                return JSONResponse({"detail": "Войдите в архив."}, status_code=401)
        if request.method == "POST" and request.headers.get("x-archive-request") != "1":
            return JSONResponse({"detail": "Недопустимый запрос."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return (Path(__file__).parent / "web" / "archive.html").read_text(encoding="utf-8")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/api/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        for key in list(failures):
            failures[key] = [t for t in failures[key] if t > now - 300]
            if not failures[key]:
                del failures[key]
        if len(failures.get(ip, [])) >= 5 or sum(map(len, failures.values())) >= 30:
            raise HTTPException(429, "Слишком много попыток. Попробуйте через пять минут.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "Слишком длинный запрос.")
        try:
            password = json.loads(body).get("password", "")
        except (ValueError, AttributeError):
            raise HTTPException(400, "Введите пароль.")
        if not isinstance(password, str) or not hmac.compare_digest(
            password.encode(), settings.archive_password.encode()
        ):
            failures.setdefault(ip, []).append(now)
            raise HTTPException(401, "Пароль не подошёл.")
        failures.pop(ip, None)
        value = f"{int(now + 43200)}.{secrets.token_hex(16)}"
        value += "." + hmac.new(signing_key, value.encode(), hashlib.sha256).hexdigest()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "archive_session",
            value,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https" or bool(os.getenv("RAILWAY_ENVIRONMENT_ID")),
            max_age=43200,
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie("archive_session")
        return response

    @app.get("/api/status")
    async def status():
        with database() as db:
            result = {
                "dialogs": db.execute("SELECT count(*) FROM dialogs").fetchone()[0],
                "messages": db.execute("SELECT count(*) FROM messages").fetchone()[0],
                "media": dict(db.execute("SELECT status,count(*) FROM messages GROUP BY status")),
                "syncing": bool(archiver and archiver.syncing),
                "sync_available": archiver is not None,
                "sync_error": archiver.sync_error if archiver else None,
            }
        return result

    @app.post("/api/sync")
    async def sync():
        if not archiver:
            raise HTTPException(409, "Для загрузки истории запустите помощника с Telegram-сессией.")
        archiver.start_sync()
        with archiver.archive.db:
            archiver.archive.db.execute(
                "UPDATE messages SET status='pending',attempts=0,error=NULL WHERE status='failed'"
            )
        archiver.wakeup.set()
        return {"ok": True}

    @app.get("/api/dialogs")
    async def dialogs(q: str = "", offset: int = 0):
        q = q[:200]
        with database() as db:
            rows = db.execute(
                """SELECT d.*, (SELECT count(*) FROM messages m WHERE m.dialog_id=d.id) AS count,
                (SELECT text FROM messages m WHERE m.dialog_id=d.id ORDER BY date DESC,id DESC LIMIT 1) AS preview
                FROM dialogs d WHERE instr(casefold(d.title),casefold(?))>0 OR EXISTS
                (SELECT 1 FROM messages m WHERE m.dialog_id=d.id AND instr(casefold(m.text),casefold(?))>0)
                ORDER BY updated DESC LIMIT 100 OFFSET ?""",
                (q, q, max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/dialogs/{dialog_id}/messages")
    async def messages(dialog_id: int, before: int = 0, q: str = ""):
        with database() as db:
            rows = db.execute(
                """SELECT dialog_id,id,date,outgoing,sender_id,text,kind,filename,mime,size,
                reply_to,grouped_id,edited,deleted,status,error FROM messages
                WHERE dialog_id=? AND (?=0 OR id<?) AND instr(casefold(text),casefold(?))>0
                ORDER BY id DESC LIMIT 60""",
                (dialog_id, before, before, q[:200]),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    @app.get("/api/dialogs/{dialog_id}/messages/{message_id}/details")
    async def details(dialog_id: int, message_id: int):
        with database() as db:
            row = db.execute(
                "SELECT metadata FROM messages WHERE dialog_id=? AND id=?", (dialog_id, message_id)
            ).fetchone()
            edits = db.execute(
                "SELECT observed,text FROM revisions WHERE dialog_id=? AND message_id=? ORDER BY observed",
                (dialog_id, message_id),
            ).fetchall()
        if not row:
            raise HTTPException(404, "Сообщение не найдено.")
        return {"metadata": json.loads(row[0]), "previous_versions": [dict(r) for r in edits]}

    @app.get("/api/media/{dialog_id}/{message_id}")
    async def media(dialog_id: int, message_id: int, download: bool = False):
        with database() as db:
            row = db.execute(
                "SELECT * FROM messages WHERE dialog_id=? AND id=?", (dialog_id, message_id)
            ).fetchone()
        if not row or row["status"] != "ready" or not row["relative_path"]:
            raise HTTPException(404, "Оригинал ещё не сохранён или недоступен.")
        root = (settings.data_dir / "archive-media").resolve()
        path = (root / row["relative_path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "Файл не найден.")
        safe_types = {
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
            "video/mp4",
            "video/webm",
            "audio/mpeg",
            "audio/ogg",
            "audio/opus",
            "audio/wav",
            "audio/mp4",
            "audio/flac",
        }
        mime = row["mime"] or "application/octet-stream"
        inline = mime in safe_types and not download
        return FileResponse(
            path,
            media_type=mime if inline else "application/octet-stream",
            filename=row["filename"] or path.name,
            content_disposition_type="inline" if inline else "attachment",
        )

    return app
