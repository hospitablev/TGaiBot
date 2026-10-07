import json
import re
import shutil
import sqlite3
import time


def text_size(content):
    if isinstance(content, str):
        return len(content)
    return sum(len(part.get("text", "")) for part in content)


def image_count(content):
    return sum(p.get("type") == "image_url" for p in content) if isinstance(content, list) else 0


def plain_text(content):
    if isinstance(content, str):
        return content
    text = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
    count = image_count(content)
    return text + (f"\n[Изображений: {count}; оригиналы сохранены локально.]" if count else "")


class History:
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.data_dir / "history.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("""CREATE TABLE IF NOT EXISTS turns (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            created REAL NOT NULL, content TEXT NOT NULL,
            UNIQUE(user_id, message_id))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS turns_user ON turns(user_id, id)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS processed (
            user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(user_id,message_id))""")
        self.db.execute("CREATE TABLE IF NOT EXISTS paused (user_id INTEGER PRIMARY KEY)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS voice (user_id INTEGER PRIMARY KEY, enabled INTEGER)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS usage (user_id INTEGER, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS controls (user_id INTEGER, created REAL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS summaries (user_id INTEGER PRIMARY KEY, through_id INTEGER, content TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS epochs (user_id INTEGER PRIMARY KEY, epoch INTEGER)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS inbox (user_id INTEGER, message_id INTEGER, text TEXT, metadata TEXT, PRIMARY KEY(user_id,message_id))"
        )
        self.db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS archive_search USING fts5(user_id UNINDEXED, turn_id UNINDEXED, text, tokenize='unicode61')"
        )
        for row_id, user_id, raw in self.db.execute(
            "SELECT id,user_id,content FROM turns WHERE id NOT IN (SELECT turn_id FROM archive_search)"
        ).fetchall():
            self.index_turn(row_id, user_id, raw)
        self.db.commit()
        self.purge()

    def purge(self):
        with self.db:
            # Correspondence stays until explicit reset; only operational counters expire.
            self.db.execute("DELETE FROM processed WHERE created < ?", (time.time() - 7 * 86400,))
            self.db.execute("DELETE FROM usage WHERE created < ?", (time.time() - 86400,))
            self.db.execute("DELETE FROM controls WHERE created < ?", (time.time() - 60,))

    def close(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()

    def seen(self, user_id, message_id):
        return (
            self.db.execute(
                "SELECT 1 FROM processed WHERE user_id=? AND message_id=?", (user_id, message_id)
            ).fetchone()
            is not None
        )

    def mark_seen(self, user_id, message_id):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO processed VALUES(?,?,?)", (user_id, message_id, time.time())
            )

    def is_paused(self, user_id):
        return (
            self.db.execute("SELECT 1 FROM paused WHERE user_id=?", (user_id,)).fetchone()
            is not None
        )

    def pause(self, user_id, paused=True):
        with self.db:
            if paused:
                self.db.execute("INSERT OR IGNORE INTO paused VALUES(?)", (user_id,))
            else:
                self.db.execute("DELETE FROM paused WHERE user_id=?", (user_id,))

    def reserve(self, user_id, now=None, min_interval=10):
        """Persistent rate/budget limits; failed requests still consume a slot."""
        now = time.time() if now is None else now
        self.purge()
        with self.db:
            recent = self.db.execute(
                "SELECT created FROM usage WHERE user_id=? AND created>?", (user_id, now - 600)
            ).fetchall()
            hourly = self.db.execute(
                "SELECT count(*) FROM usage WHERE user_id=? AND created>?", (user_id, now - 3600)
            ).fetchone()[0]
            daily = self.db.execute(
                "SELECT count(*) FROM usage WHERE created>?", (now - 86400,)
            ).fetchone()[0]
            if (
                len(recent) >= 6
                or hourly >= 30
                or daily >= 200
                or any(now - row[0] < min_interval for row in recent)
            ):
                return False
            self.db.execute("INSERT INTO usage VALUES(?,?)", (user_id, now))
        return True

    def set_voice(self, user_id, enabled):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO voice VALUES(?,?)", (user_id, int(enabled)))

    def reserve_control(self, user_id):
        now = time.time()
        with self.db:
            count = self.db.execute(
                "SELECT count(*) FROM controls WHERE user_id=? AND created>?", (user_id, now - 60)
            ).fetchone()[0]
            if count >= 6:
                return False
            self.db.execute("INSERT INTO controls VALUES(?,?)", (user_id, now))
        return True

    def voice_enabled(self, user_id, default=False):
        row = self.db.execute("SELECT enabled FROM voice WHERE user_id=?", (user_id,)).fetchone()
        return bool(row[0]) if row else default

    def messages(self, user_id, current=None):
        return [message for _, turn in self.recent(user_id, current) for message in turn]

    def recent(self, user_id, current=None):
        self.purge()
        rows = self.db.execute(
            "SELECT id,content FROM turns WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, self.settings.history_turns),
        ).fetchall()
        selected = []
        chars = text_size(current) if current is not None else 0
        images = image_count(current) if current is not None else 0
        size = len(json.dumps(current).encode()) if current is not None else 0
        for row_id, raw in rows:
            turn = json.loads(raw)
            turn_images = sum(image_count(m["content"]) for m in turn)
            if (
                images + turn_images > self.settings.history_images
                or size + len(raw.encode()) > self.settings.history_bytes
            ):
                turn = [{"role": m["role"], "content": plain_text(m["content"])} for m in turn]
            turn_chars = sum(text_size(m["content"]) for m in turn)
            if chars + turn_chars > self.settings.history_chars:
                break
            chars += turn_chars
            images += sum(image_count(m["content"]) for m in turn)
            size += len(json.dumps(turn).encode())
            selected.append((row_id, turn))
        return list(reversed(selected))

    def index_turn(self, row_id, user_id, raw):
        text = "\n".join(m["role"] + ": " + plain_text(m["content"]) for m in json.loads(raw))
        self.db.execute(
            "INSERT INTO archive_search(user_id,turn_id,text) VALUES(?,?,?)",
            (user_id, row_id, text),
        )

    def add(self, user_id, message_id, content, answer):
        raw = json.dumps(
            [{"role": "user", "content": content}, {"role": "assistant", "content": answer}],
            ensure_ascii=False,
        )
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO turns(user_id,message_id,created,content) VALUES(?,?,?,?)",
                (user_id, message_id, time.time(), raw),
            )
            if result.rowcount:
                self.index_turn(result.lastrowid, user_id, raw)

    def archive_incoming(self, user_id, message):
        metadata = {
            "date": message.date.isoformat(),
            "filename": getattr(getattr(message, "file", None), "name", None),
        }
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?)",
                (
                    user_id,
                    message.id,
                    message.message or "",
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )

    def archive_attachment(self, user_id, message_id, path):
        directory = self.settings.data_dir / "attachments" / str(int(user_id))
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, directory / (str(int(message_id)) + path.suffix))

    def epoch(self, user_id):
        row = self.db.execute("SELECT epoch FROM epochs WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row else 0

    def summary(self, user_id):
        row = self.db.execute(
            "SELECT through_id,content FROM summaries WHERE user_id=?", (user_id,)
        ).fetchone()
        return (row[0], json.loads(row[1])) if row else (0, {})

    def save_summary(self, user_id, through_id, content, epoch):
        with self.db:
            if self.epoch(user_id) != epoch:
                return False
            self.db.execute(
                "INSERT OR REPLACE INTO summaries VALUES(?,?,?)",
                (user_id, through_id, json.dumps(content, ensure_ascii=False)),
            )
        return True

    def retrieve(self, user_id, query, exclude_ids=(), limit=4):
        words = list(dict.fromkeys(re.findall(r"[\w]{3,}", query.lower(), flags=re.UNICODE)))[:20]
        if not words:
            return []
        expression = " OR ".join('"' + w.replace('"', "") + '"*' for w in words)
        rows = self.db.execute(
            "SELECT turn_id,text FROM archive_search WHERE archive_search MATCH ? AND user_id=? ORDER BY rank LIMIT 40",
            (expression, user_id),
        ).fetchall()
        return [(row_id, text) for row_id, text in rows if row_id not in exclude_ids][:limit]

    def reset(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM turns WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM archive_search WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM inbox WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM summaries WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM voice WHERE user_id=?", (user_id,))
            self.db.execute(
                "INSERT INTO epochs VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET epoch=epoch+1",
                (user_id,),
            )
        root = (self.settings.data_dir / "attachments").resolve()
        directory = (root / str(int(user_id))).resolve()
        if directory.parent != root:
            raise ValueError("Invalid attachment directory")
        if directory.exists():
            shutil.rmtree(directory)
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
