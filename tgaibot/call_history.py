"""Observed call facts. Never invent a reason from the absence of a record."""

import time
from datetime import datetime, timedelta, timezone

REASONS = {
    "disabled": "приём звонков был выключен",
    "unavailable": "голосовой модуль был недоступен",
    "not_allowed": "звонящий не входил в разрешённый список",
    "busy": "уже шёл другой звонок",
    "paused": "собеседник ранее приостановил ответы",
    "limited": "сработал лимит запросов",
    "restart": "процесс перезапустился во время звонка",
    "connection": "сбой подключения",
    "processing": "сбой обработки речи",
    "timeout": "истёк лимит времени звонка",
    "offline_unknown": "Telegram сообщает о пропущенном звонке, точная причина неизвестна",
    "ended": "звонок завершён",
    "unknown": "точная причина неизвестна",
}


class CallHistory:
    def __init__(self, db):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS call_events (
            id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE, user_id INTEGER NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT 'unknown', duration INTEGER NOT NULL DEFAULT 0,
            turns INTEGER NOT NULL DEFAULT 0, interruptions INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS call_person ON call_events(user_id,created);
        CREATE TABLE IF NOT EXISTS call_turns(id INTEGER PRIMARY KEY,call_id INTEGER NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,created REAL NOT NULL,interrupted INTEGER NOT NULL DEFAULT 0);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(call_events)")}
        if "recording" not in columns:
            db.execute("ALTER TABLE call_events ADD COLUMN recording TEXT")
        with db:
            db.execute(
                "UPDATE call_events SET status='failed',reason='restart',updated=? WHERE status IN ('ringing','connecting','active')",
                (time.time(),),
            )

    def incoming(self, user_id, telegram_id=None, created=None):
        if telegram_id is not None:
            row = self.db.execute(
                "SELECT id FROM call_events WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if not row:
                row = self.db.execute(
                    "SELECT id FROM call_events WHERE user_id=? AND telegram_id IS NULL AND created BETWEEN ? AND ? ORDER BY id DESC LIMIT 1",
                    (user_id, (created or time.time()) - 30, (created or time.time()) + 30),
                ).fetchone()
                if row:
                    with self.db:
                        self.db.execute(
                            "UPDATE call_events SET telegram_id=? WHERE id=?", (telegram_id, row[0])
                        )
        else:
            row = self.db.execute(
                "SELECT id FROM call_events WHERE user_id=? AND status='ringing' AND created>? ORDER BY id DESC LIMIT 1",
                (user_id, time.time() - 90),
            ).fetchone()
        if row:
            return row[0]
        with self.db:
            return self.db.execute(
                "INSERT INTO call_events(telegram_id,user_id,created,updated,status) VALUES(?,?,?,?, 'ringing')",
                (telegram_id, user_id, created or time.time(), time.time()),
            ).lastrowid

    def update(self, identifier, status, reason="unknown"):
        with self.db:
            self.db.execute(
                "UPDATE call_events SET status=?,reason=?,updated=? WHERE id=?",
                (status, reason, time.time(), identifier),
            )

    def service(self, user_id, message):
        action = getattr(message, "action", None)
        if type(action).__name__ != "MessageActionPhoneCall" or getattr(message, "out", False):
            return
        identifier = self.incoming(user_id, action.call_id, message.date.timestamp())
        old = self.db.execute(
            "SELECT reason,status FROM call_events WHERE id=?", (identifier,)
        ).fetchone()
        duration = getattr(action, "duration", 0) or 0
        reason = (
            old[0]
            if old[0] not in {"unknown", "ended"}
            else "ended"
            if duration
            else "offline_unknown"
        )
        status = "ended" if duration else "missed"
        # Preserve a diagnosed failure; Telegram's service entry adds duration, not a diagnosis.
        if old[1] in {"failed", "declined"}:
            status = old[1]
        self.update(identifier, status, reason)
        with self.db:
            self.db.execute("UPDATE call_events SET duration=? WHERE id=?", (duration, identifier))

    def turn(self, identifier, latency_ms=0, interrupted=False):
        with self.db:
            self.db.execute(
                "UPDATE call_events SET turns=turns+?,interruptions=interruptions+?,latency_ms=latency_ms+? WHERE id=?",
                (int(not interrupted), int(interrupted), int(latency_ms), identifier),
            )

    def transcript(self, identifier, role, text, interrupted=False):
        with self.db:
            self.db.execute(
                "INSERT INTO call_turns(call_id,role,text,created,interrupted) VALUES(?,?,?,?,?)",
                (identifier, role, text, time.time(), int(interrupted)),
            )

    def recording(self, identifier, path, duration):
        with self.db:
            self.db.execute(
                "UPDATE call_events SET recording=?,duration=? WHERE id=?",
                (str(path), int(duration), identifier),
            )

    def list(self, user_id, before=0):
        names = [r[1] for r in self.db.execute("PRAGMA table_info(call_events)")]
        result = []
        for row in self.db.execute(
            "SELECT * FROM call_events WHERE user_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT 30",
            (user_id, before, before),
        ):
            item = dict(zip(names, row))
            item["reason_text"] = REASONS.get(item["reason"], REASONS["unknown"])
            item["has_recording"] = bool(item.pop("recording", None))
            item["transcript"] = [
                {"role": r[0], "text": r[1], "created": r[2], "interrupted": bool(r[3])}
                for r in self.db.execute(
                    "SELECT role,text,created,interrupted FROM call_turns WHERE call_id=? ORDER BY id LIMIT 500",
                    (item["id"],),
                )
            ]
            result.append(item)
        return result

    def context(self, user_id):
        rows = self.db.execute(
            "SELECT created,status,reason,duration FROM call_events WHERE user_id=? ORDER BY created DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        if not rows:
            return ""
        lines = ["Реальные события звонков этого собеседника (факты журнала):"]
        for stamp, status, reason, duration in rows:
            date = datetime.fromtimestamp(stamp, timezone(timedelta(hours=5))).isoformat(
                timespec="minutes"
            )
            lines.append(
                f"{date}: {status}; {REASONS.get(reason, REASONS['unknown'])}; длительность {duration} с."
            )
        lines.append(
            "Если спрашивают, почему не взял трубку, опирайся на этот журнал. При неизвестной причине так и скажи. Не выдумывай человеческую занятость, сон, прогулки или дела. Можно тепло извиниться и предложить повторить звонок. Пропущенный звонок не поручение перезванивать."
        )
        return "\n".join(lines)
