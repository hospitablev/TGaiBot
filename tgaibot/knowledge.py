"""Per-person facts with provenance, versions, conflicts and explicit forgetting."""

import json
import re
import time
from datetime import date, datetime, timedelta, timezone

LOCAL = timezone(timedelta(hours=5))
PROFILE = {
    "first_name": "Имя",
    "last_name": "Фамилия",
    "nickname": "Как обращаться",
    "birth_date": "Дата рождения",
    "age": "Возраст со слов собеседника",
    "city": "Город",
    "country": "Страна",
    "occupation": "Занятие",
    "language": "Язык общения",
    "timezone": "Часовой пояс",
}
CATEGORIES = {"profile", "preference", "relationship", "project", "plan"}
CATEGORY_LABELS = {
    "preference": "Предпочтение",
    "relationship": "Близкие",
    "project": "Проект",
    "plan": "План",
}
CORRECTION = re.compile(
    r"исправ|теперь|на самом деле|замени|переех|смени|больше не|уже|не .+? а |отмен|заверш|закончил",
    re.I,
)
REMEMBER = re.compile(r"\b(?:запомни|сохрани в память|remember)\b", re.I)
SECRET = re.compile(r"(?:парол[ьья]|api[_ -]?key|токен|password|код (?:входа|подтверждения))", re.I)


def words(text):
    return {word[:5] for word in re.findall(r"[\w]{3,}", text.lower())}


def explicit_birth_date(value, quote):
    born = date.fromisoformat(value)
    if value in quote:
        return True
    for day, month, year in re.findall(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", quote):
        if (int(year), int(month), int(day)) == (born.year, born.month, born.day):
            return True
    months = (
        "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
    )
    for day, month, year in re.findall(r"\b(\d{1,2})\s+([а-я]+)\s+(\d{4})\b", quote.lower()):
        if month in months and (int(year), months.index(month) + 1, int(day)) == (
            born.year,
            born.month,
            born.day,
        ):
            return True
    return False


class Knowledge:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_sources (
                user_id INTEGER, message_id INTEGER, text TEXT NOT NULL, created REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id,message_id));
            CREATE INDEX IF NOT EXISTS memory_queue ON memory_sources(state,retry_at,created);
            CREATE TABLE IF NOT EXISTS memory_facts (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, slot TEXT NOT NULL,
                value TEXT NOT NULL, quote TEXT NOT NULL, source_id INTEGER NOT NULL,
                observed REAL NOT NULL, status TEXT NOT NULL, expires REAL,
                tags TEXT NOT NULL, supersedes INTEGER);
            CREATE INDEX IF NOT EXISTS memory_person ON memory_facts(user_id,slot,status);
            CREATE TABLE IF NOT EXISTS memory_blocks (
                user_id INTEGER, slot TEXT, created REAL NOT NULL, PRIMARY KEY(user_id,slot));
            CREATE TABLE IF NOT EXISTS memory_state (user_id INTEGER PRIMARY KEY, recall_after REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_withdrawals (user_id INTEGER,slot TEXT,PRIMARY KEY(user_id,slot));
        """)
        for table, column, definition in (
            ("memory_sources", "origin", "TEXT NOT NULL DEFAULT 'text'"),
            ("memory_facts", "confirmation_id", "INTEGER"),
        ):
            if column not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        db.commit()

    def enqueue(self, user_id, message_id, text, *, created=None, origin="text"):
        text = text.strip()
        # Ignore greetings/acknowledgments, not short facts like "Мне 22".
        if (
            len(text) < 4
            or len(text) > 12000
            or text.startswith("/")
            or text.lower().strip(".!? 🙂")
            in {
                "привет",
                "здравствуйте",
                "спасибо",
                "хорошо",
                "понятно",
                "продолжай",
                "доброе утро",
            }
        ):
            return False
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO memory_sources(user_id,message_id,text,created,origin) VALUES(?,?,?,?,?)",
                (user_id, message_id, text, created or time.time(), origin),
            )
        return True

    def pending(self):
        # No persistent "running" state: an interrupted read can safely be retried.
        first = self.db.execute(
            "SELECT user_id FROM memory_sources WHERE state='pending' AND retry_at<=? ORDER BY created LIMIT 1",
            (time.time(),),
        ).fetchone()
        if not first:
            return None, []
        rows = self.db.execute(
            "SELECT message_id,text,created,version,origin FROM memory_sources WHERE user_id=? AND state='pending' AND retry_at<=? ORDER BY created,message_id LIMIT 6",
            (first[0], time.time()),
        ).fetchall()
        batch, size = [], 0
        for mid, text, created, version, origin in rows:
            if batch and size + len(text) > 16000:
                break
            batch.append(
                {
                    "message_id": mid,
                    "text": text,
                    "created": created,
                    "version": version,
                    "origin": origin,
                }
            )
            size += len(text)
        return first[0], batch

    def validate(self, facts, batch):
        if not isinstance(facts, list) or len(facts) > 24:
            raise ValueError("Invalid fact list")
        sources = {s["message_id"]: s for s in batch}
        result, seen = [], set()
        for fact in facts:
            if not isinstance(fact, dict) or set(fact) != {
                "source_id",
                "slot",
                "value",
                "quote",
                "operation",
                "tags",
            }:
                raise ValueError("Invalid fact schema")
            if type(fact["source_id"]) is not int or fact["source_id"] not in sources:
                raise ValueError("Unknown source")
            slot, value, quote = (fact[k] for k in ("slot", "value", "quote"))
            if not all(isinstance(v, str) for v in (slot, value, quote)):
                raise ValueError("Invalid fact text")
            if (
                not re.fullmatch(r"[a-z]+\.[a-z][a-z0-9_]{0,55}", slot)
                or slot.split(".")[0] not in CATEGORIES
            ):
                raise ValueError("Invalid slot")
            if slot.startswith("profile.") and slot.split(".")[1] not in PROFILE:
                raise ValueError("Unknown profile field")
            source = sources[fact["source_id"]]
            if not (1 <= len(value) <= 400 and 4 <= len(quote) <= 1200 and quote in source["text"]):
                raise ValueError("Unverifiable quote")
            if fact["operation"] not in {"assert", "correct"}:
                raise ValueError("Invalid operation")
            if (
                not isinstance(fact["tags"], list)
                or len(fact["tags"]) > 8
                or any(not isinstance(t, str) or not 1 <= len(t) <= 40 for t in fact["tags"])
            ):
                raise ValueError("Invalid tags")
            if SECRET.search(quote) or re.search(r"\b[\w-]{40,}\b", value):
                continue
            if (
                slot in {"profile.first_name", "profile.last_name", "profile.nickname"}
                and value.casefold() not in quote.casefold()
            ):
                continue
            if slot == "profile.age":
                if (
                    not value.isdecimal()
                    or not 0 <= int(value) <= 120
                    or not re.search(r"(?<!\d)" + re.escape(value) + r"(?!\d)", quote)
                ):
                    continue
            if slot == "profile.birth_date":
                try:
                    born = date.fromisoformat(value)
                    if (
                        born > datetime.fromtimestamp(source["created"], LOCAL).date()
                        or born.year < 1900
                        or str(born.year) not in quote
                        or not explicit_birth_date(value, quote)
                    ):
                        continue
                except ValueError:
                    continue
            key = (fact["source_id"], slot)
            if key in seen:
                raise ValueError("Conflicting facts in one message")
            seen.add(key)
            result.append(fact)
        return result

    def apply(self, user_id, batch, facts, *, epoch, history):
        facts = self.validate(facts, batch)
        sources = {s["message_id"]: s for s in batch}
        with self.db:
            if history.epoch(user_id) != epoch:
                return False
            for source in batch:
                row = self.db.execute(
                    "SELECT version,state FROM memory_sources WHERE user_id=? AND message_id=?",
                    (user_id, source["message_id"]),
                ).fetchone()
                if row != (source["version"], "pending"):
                    return False
            for fact in sorted(
                facts, key=lambda f: (sources[f["source_id"]]["created"], f["source_id"])
            ):
                source = sources[fact["source_id"]]
                newer = self.db.execute(
                    "SELECT 1 FROM memory_facts WHERE user_id=? AND slot=? AND (observed>? OR (observed=? AND source_id>?)) LIMIT 1",
                    (
                        user_id,
                        fact["slot"],
                        source["created"],
                        source["created"],
                        fact["source_id"],
                    ),
                ).fetchone()
                # A delayed retry must not roll a profile back to an older message.
                if newer:
                    continue
                blocked = self.db.execute(
                    "SELECT created FROM memory_blocks WHERE user_id=? AND slot=?",
                    (user_id, fact["slot"]),
                ).fetchone()
                if blocked:
                    if source["created"] <= blocked[0] or not REMEMBER.search(source["text"]):
                        continue
                    self.db.execute(
                        "DELETE FROM memory_blocks WHERE user_id=? AND slot=?",
                        (user_id, fact["slot"]),
                    )
                self.db.execute(
                    "DELETE FROM memory_withdrawals WHERE user_id=? AND slot=?",
                    (user_id, fact["slot"]),
                )
                old = self.db.execute(
                    "SELECT id,value FROM memory_facts WHERE user_id=? AND slot=? AND status IN ('active','conflict') ORDER BY id DESC",
                    (user_id, fact["slot"]),
                ).fetchall()
                different = any(v.casefold() != fact["value"].casefold() for _, v in old)
                correction = fact["operation"] == "correct" and CORRECTION.search(source["text"])
                state = "conflict" if different and not correction else "active"
                if source.get("origin") == "voice":
                    state = "unconfirmed"
                else:
                    self.db.execute(
                        "UPDATE memory_facts SET status='superseded' WHERE user_id=? AND slot=? AND status='unconfirmed'",
                        (user_id, fact["slot"]),
                    )
                    self.db.execute(
                        "UPDATE memory_facts SET status=? WHERE user_id=? AND slot=? AND status IN ('active','conflict')",
                        (
                            "conflict" if state == "conflict" else "superseded",
                            user_id,
                            fact["slot"],
                        ),
                    )
                category = fact["slot"].split(".")[0]
                days = (
                    30
                    if category == "plan"
                    else 180
                    if category in {"project", "relationship"}
                    or fact["slot"] in {"profile.city", "profile.occupation"}
                    else None
                )
                expires = source["created"] + days * 86400 if days else None
                self.db.execute(
                    "INSERT INTO memory_facts(user_id,slot,value,quote,source_id,observed,status,expires,tags,supersedes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id,
                        fact["slot"],
                        fact["value"],
                        fact["quote"],
                        fact["source_id"],
                        source["created"],
                        state,
                        expires,
                        json.dumps(fact["tags"], ensure_ascii=False),
                        old[0][0] if old else None,
                    ),
                )
            for source in batch:
                self.db.execute(
                    "UPDATE memory_sources SET state='done',attempts=0 WHERE user_id=? AND message_id=?",
                    (user_id, source["message_id"]),
                )
        return True

    def failed(self, user_id, batch):
        with self.db:
            for source in batch:
                self.db.execute(
                    "UPDATE memory_sources SET attempts=attempts+1,retry_at=?,state=CASE WHEN attempts>=2 THEN 'failed' ELSE 'pending' END WHERE user_id=? AND message_id=? AND version=? AND state='pending'",
                    (time.time() + 300, user_id, source["message_id"], source["version"]),
                )

    def invalidate(self, user_id, source_id, replacement=None):
        # Called within History's transaction. Never reactivate an older superseded version.
        self.db.execute(
            "INSERT OR IGNORE INTO memory_withdrawals SELECT user_id,slot FROM memory_facts WHERE user_id=? AND (source_id=? OR confirmation_id=?) AND status IN ('active','conflict','unconfirmed')",
            (user_id, source_id, source_id),
        )
        self.db.execute(
            "UPDATE memory_facts SET status='revoked' WHERE user_id=? AND (source_id=? OR confirmation_id=?)",
            (user_id, source_id, source_id),
        )
        if replacement is None:
            self.db.execute(
                "DELETE FROM memory_sources WHERE user_id=? AND message_id=?", (user_id, source_id)
            )
        else:
            self.db.execute(
                "UPDATE memory_sources SET text=?,version=version+1,state='pending',attempts=0,retry_at=0,created=? WHERE user_id=? AND message_id=?",
                (replacement[:12000], time.time(), user_id, source_id),
            )

    def facts(self, user_id, *, query="", limit=30, now=None):
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT id,slot,value,quote,source_id,observed,status,expires,tags FROM memory_facts WHERE user_id=? AND status IN ('active','conflict','unconfirmed') ORDER BY CASE WHEN slot LIKE 'profile.%' THEN 0 ELSE 1 END,observed DESC,id DESC LIMIT 500",
            (user_id,),
        ).fetchall()
        records = []
        terms = words(query)
        for row in rows:
            item = dict(
                zip(
                    (
                        "id",
                        "slot",
                        "value",
                        "quote",
                        "source_id",
                        "observed",
                        "status",
                        "expires",
                        "tags",
                    ),
                    row,
                )
            )
            item["tags"] = json.loads(item["tags"])
            item["stale"] = bool(item["expires"] and item["expires"] < now)
            item["observed_on"] = datetime.fromtimestamp(item["observed"], LOCAL).isoformat(
                timespec="minutes"
            )
            item["label"] = PROFILE.get(
                item["slot"].removeprefix("profile."),
                CATEGORY_LABELS.get(item["slot"].split(".")[0], "Сведение")
                + (" · " + item["tags"][0] if item["tags"] else ""),
            )
            if (
                item["slot"] == "profile.birth_date"
                and item["status"] == "active"
                and "profile.age" not in self.withdrawn(user_id)
            ):
                born, today = (
                    date.fromisoformat(item["value"]),
                    datetime.fromtimestamp(now, LOCAL).date(),
                )
                item["calculated_age"] = (
                    today.year - born.year - ((today.month, today.day) < (born.month, born.day))
                )
            score = len(
                terms & words(item["value"] + " " + item["label"] + " " + " ".join(item["tags"]))
            )
            pinned = item["slot"].startswith(("profile.", "preference."))
            if not query or score or pinned:
                records.append(
                    (
                        score + (3 if item["slot"].startswith("profile.") else 1 if pinned else 0),
                        item,
                    )
                )
        records.sort(key=lambda pair: (-pair[0], -pair[1]["observed"], -pair[1]["id"]))
        births = [
            f for _, f in records if f["slot"] == "profile.birth_date" and f["status"] == "active"
        ]
        ages = [f for _, f in records if f["slot"] == "profile.age" and f["status"] == "active"]
        if len(births) == 1 and len(ages) == 1:
            born, at = (
                date.fromisoformat(births[0]["value"]),
                datetime.fromtimestamp(ages[0]["observed"], LOCAL).date(),
            )
            expected = at.year - born.year - ((at.month, at.day) < (born.month, born.day))
            if expected != int(ages[0]["value"]):
                births[0]["status"] = ages[0]["status"] = "conflict"
                births[0].pop("calculated_age", None)
        return [item for _, item in records[:limit]]

    def display(self, user_id):
        facts = self.facts(user_id)
        pending = self.db.execute(
            "SELECT count(*) FROM memory_sources WHERE user_id=? AND state='pending'", (user_id,)
        ).fetchone()[0]
        failed = self.db.execute(
            "SELECT count(*) FROM memory_sources WHERE user_id=? AND state='failed'", (user_id,)
        ).fetchone()[0]
        lines = ["Вот что сохранено в моей памяти о тебе:"]
        for f in facts:
            state = (
                "; нужно уточнить противоречие"
                if f["status"] == "conflict"
                else "; могло устареть"
                if f["stale"]
                else ""
            )
            if f["status"] == "unconfirmed":
                state = "; из голосового, требует подтверждения"
            lines.append(
                f"#{f['id']} · {f['label']}: {f['value']} (сообщено {f['observed_on'][:10]}{state})"
            )
        if not facts:
            lines.append("Отдельных фактов пока нет. Свежую переписку я всё равно учитываю.")
        if pending:
            lines.append(f"Ещё сообщений на разборе: {pending}.")
        if failed:
            lines.append(f"Не удалось разобрать сообщений: {failed}. Повторить: «Обнови память».")
        lines.append(
            "Удалить отдельный факт: «Забудь факт 12». Проверенное сведение из голосового: «Подтверди факт 12». Всю память: «Очисти свою память обо мне». Личный архив владельца хранится отдельно."
        )
        return "\n\n".join(lines)

    def forget(self, user_id, fact_id, history):
        row = self.db.execute(
            "SELECT slot FROM memory_facts WHERE user_id=? AND id=? AND status IN ('active','conflict','unconfirmed')",
            (user_id, fact_id),
        ).fetchone()
        if not row:
            raise ValueError("Такого факта в твоей памяти нет. Попроси показать память.")
        slot = row[0]
        return self.forget_slot(user_id, slot, history)

    def forget_slot(self, user_id, slot, history):
        ids = [
            r[0]
            for r in self.db.execute(
                "SELECT DISTINCT source_id FROM memory_facts WHERE user_id=? AND slot=?",
                (user_id, slot),
            )
        ]
        # All related original turns are removed from AI recall, but the archive is untouched.
        history.revise(ids, user_id)
        with self.db:
            self.db.execute("DELETE FROM memory_facts WHERE user_id=? AND slot=?", (user_id, slot))
            self.db.execute(
                "INSERT OR REPLACE INTO memory_blocks VALUES(?,?,?)", (user_id, slot, time.time())
            )
            self.db.execute(
                "INSERT OR REPLACE INTO memory_state VALUES(?,?)", (user_id, time.time())
            )
            self.db.execute("DELETE FROM summaries WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM memory_sources WHERE user_id=?", (user_id,))
            self.db.execute(
                "INSERT INTO epochs VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET epoch=epoch+1",
                (user_id,),
            )
        return slot

    def withdrawn(self, user_id):
        return [
            row[0]
            for row in self.db.execute(
                "SELECT slot FROM memory_withdrawals WHERE user_id=? UNION SELECT slot FROM memory_blocks WHERE user_id=?",
                (user_id, user_id),
            )
        ]

    def recall_after(self, user_id):
        row = self.db.execute(
            "SELECT recall_after FROM memory_state WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else -1

    def confirm(self, user_id, fact_id, confirmation_id):
        row = self.db.execute(
            "SELECT slot FROM memory_facts WHERE user_id=? AND id=? AND status='unconfirmed'",
            (user_id, fact_id),
        ).fetchone()
        if not row:
            raise ValueError("Такого неподтверждённого факта нет. Попроси показать память.")
        if self.db.execute(
            "SELECT 1 FROM memory_blocks WHERE user_id=? AND slot=?", (user_id, row[0])
        ).fetchone():
            raise ValueError("Это поле было забыто. Сообщи его заново со словом «Запомни».")
        with self.db:
            self.db.execute(
                "UPDATE memory_facts SET status='superseded' WHERE user_id=? AND slot=? AND status IN ('active','conflict','unconfirmed')",
                (user_id, row[0]),
            )
            self.db.execute(
                "UPDATE memory_facts SET status='active',confirmation_id=? WHERE user_id=? AND id=?",
                (confirmation_id, user_id, fact_id),
            )
            self.db.execute(
                "DELETE FROM memory_withdrawals WHERE user_id=? AND slot=?", (user_id, row[0])
            )

    def reset(self, user_id):
        for table in (
            "memory_facts",
            "memory_sources",
            "memory_blocks",
            "memory_state",
            "memory_withdrawals",
        ):
            self.db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
