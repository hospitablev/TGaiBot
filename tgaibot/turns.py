"""Durable conversation turns and a Telegram outbox with stable random IDs."""

import asyncio
import hashlib
import json
import logging
import time
from contextvars import ContextVar
from types import SimpleNamespace

from telethon import errors, events, functions
from telethon.extensions import BinaryReader

from .conversation import control_intent

LOG = logging.getLogger(__name__)
CURRENT_TURN = ContextVar("current_conversation_turn", default=None)
SEND_TYPES = (
    functions.messages.SendMessageRequest,
    functions.messages.SendMediaRequest,
    functions.messages.SendMultiMediaRequest,
)


class RetryTurn(Exception):
    def __init__(self, delay):
        self.delay = delay


class DeliveryUncertain(Exception):
    pass


def unpack(data):
    with BinaryReader(data) as reader:
        return reader.tgread_object()


def restored_event(message, client):
    event = events.NewMessage.Event(message)
    event._entities = {
        entity.id: entity for entity in (message.sender, message.chat) if entity is not None
    }
    event._set_client(client)
    return event


class Turn:
    def __init__(self, queue, row):
        self.queue, self.db = queue, queue.db
        self.id, self.user, self.ids, self.epoch = row[0], row[1], json.loads(row[2]), row[3]
        self.data = json.loads(row[4])
        self.sealed = bool(self.data.get("sealed"))
        self.scope, self.send_index = "final", 0

    def save(self):
        with self.db:
            self.db.execute(
                "UPDATE reply_jobs SET data=?,updated=? WHERE id=?",
                (json.dumps(self.data, ensure_ascii=False), time.time(), self.id),
            )

    def seal(self):
        self.sealed = True
        self.data["sealed"] = True
        self.save()

    def sending(self, scope):
        self.seal()
        self.scope, self.send_index = scope, 0

    async def step(self, number, produce):
        steps = self.data.setdefault("steps", {})
        if str(number) not in steps:
            response = await produce()
            steps[str(number)] = response
            self.save()
        return steps[str(number)]

    async def tool(self, name, args, places, produce):
        self.seal()
        key = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
        cached = self.data.setdefault("tools", {}).get(key)
        if cached is not None:
            places.update(cached["places"])
            return cached["result"]
        self.sending("tool:" + key)
        result = await produce()
        self.remember_tool(name, args, places, result)
        return result

    def remember_tool(self, name, args, places, result):
        key = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()
        self.data.setdefault("tools", {})[key] = {"result": result, "places": places.copy()}
        self.save()

    async def rpc(self, request, send):
        self.seal()
        key = f"{self.scope}:{self.send_index}"
        self.send_index += 1
        row = self.db.execute(
            "SELECT request,response FROM reply_outbox WHERE job=? AND slot=?", (self.id, key)
        ).fetchone()
        if row:
            previous = unpack(row[0])
            if type(previous) is not type(request) or getattr(previous, "message", None) != getattr(
                request, "message", None
            ):
                raise DeliveryUncertain("Resumed delivery differs from its saved payload")
        if row and row[1] is not None:
            return unpack(row[1])
        if row:
            request = unpack(row[0])
        else:
            with self.db:
                self.db.execute(
                    "INSERT INTO reply_outbox VALUES(?,?,?,NULL)", (self.id, key, bytes(request))
                )
        try:
            response = await send(request)
        except errors.RandomIdDuplicateError as exc:
            # Telegram knows the random ID but cannot return a usable receipt.
            # Do not generate a new ID and risk sending the same message twice.
            raise DeliveryUncertain("Telegram already knows the delivery ID") from exc
        with self.db:
            self.db.execute(
                "UPDATE reply_outbox SET response=? WHERE job=? AND slot=?",
                (bytes(response), self.id, key),
            )
        return response


class TurnQueue:
    QUIET = 1.2
    MAX_WAIT = 4.0

    def __init__(self, bot):
        self.bot, self.db = bot, bot.history.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS reply_jobs(
            id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,ids TEXT NOT NULL,epoch INTEGER NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',
            created REAL NOT NULL,updated REAL NOT NULL,due REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reply_sources(user_id INTEGER,message_id INTEGER,job INTEGER,
            PRIMARY KEY(user_id,message_id));
        CREATE TABLE IF NOT EXISTS reply_outbox(job INTEGER,slot TEXT,request BLOB,response BLOB,
            PRIMARY KEY(job,slot));
        CREATE TABLE IF NOT EXISTS reply_meta(key TEXT PRIMARY KEY,value REAL);
        """)
        with self.db:
            self.db.execute("UPDATE reply_jobs SET status='pending' WHERE status='running'")
        self.live = {}
        self.runners = {}
        self.turns = {}
        self.merge_requested = set()
        self.waiters = {}
        self.closed = False
        self.wakeup = asyncio.Event()
        self.scheduler = None
        self.recovery = None
        self.caught_up = False
        self.last_watermark = 0

    @staticmethod
    def mergeable(event, messages):
        text = event.raw_text or ""
        return (
            len(messages) == 1
            and bool(text.strip())
            and len(text) <= 1000
            and not getattr(messages[0], "media", None)
            and not getattr(messages[0], "reply_to", None)
            and not control_intent(text).startswith("/")
            and not text.startswith("/")
        )

    def enqueue(self, event, messages):
        uid, now = event.sender_id, time.time()
        ids = [m.id for m in messages]
        for mid in ids:
            row = self.db.execute(
                "SELECT job FROM reply_sources WHERE user_id=? AND message_id=?", (uid, mid)
            ).fetchone()
            if row:
                return row[0]
        if all(self.bot.history.seen(uid, mid) for mid in ids):
            return None
        merge = self.mergeable(event, messages)
        previous = self.db.execute(
            "SELECT id,ids,data,status,created FROM reply_jobs WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (uid,),
        ).fetchone()
        target = None
        if previous and merge:
            data = json.loads(previous[2])
            age = now - previous[4]
            active = self.turns.get(uid)
            can_restart = (
                previous[3] == "running"
                and active
                and not active.sealed
                and data.get("replans", 0) < 2
                and age < 8
            )
            if (
                (previous[3] == "pending" or can_restart)
                and data.get("mergeable")
                and not data.get("sealed")
                and len(json.loads(previous[1])) < 12
                and data.get("chars", 0) + len(event.raw_text) <= 4000
                and age < 8
            ):
                target = previous[0]
                merged = json.loads(previous[1]) + ids
                data = {
                    "mergeable": True,
                    "chars": data.get("chars", 0) + len(event.raw_text),
                    "reserved": data.get("reserved", False),
                    "replans": data.get("replans", 0) + int(bool(can_restart)),
                }
                with self.db:
                    self.db.execute(
                        "UPDATE reply_jobs SET ids=?,data=?,updated=?,due=? WHERE id=?",
                        (
                            json.dumps(merged),
                            json.dumps(data),
                            now,
                            min(now + self.QUIET, previous[4] + self.MAX_WAIT),
                            target,
                        ),
                    )
                if can_restart:
                    self.merge_requested.add(target)
                    self.runners[uid].cancel()
        if target is None:
            data = {
                "mergeable": merge,
                "chars": len(event.raw_text or ""),
                "control": control_intent(event.raw_text or "").startswith("/"),
            }
            with self.db:
                target = self.db.execute(
                    "INSERT INTO reply_jobs(user_id,ids,epoch,data,created,updated,due) VALUES(?,?,?,?,?,?,?)",
                    (
                        uid,
                        json.dumps(ids),
                        self.bot.history.epoch(uid),
                        json.dumps(data),
                        now,
                        now,
                        now + self.QUIET if merge else now,
                    ),
                ).lastrowid
        with self.db:
            for message in messages:
                self.db.execute(
                    "INSERT INTO reply_sources VALUES(?,?,?)", (uid, message.id, target)
                )
                self.live[(uid, message.id)] = (event, message)
        while len(self.live) > 500:
            self.live.pop(next(iter(self.live)))
        self.wakeup.set()
        return target

    async def submit(self, event, messages):
        target = self.enqueue(event, messages)
        if target is None:
            return
        status = self.db.execute("SELECT status FROM reply_jobs WHERE id=?", (target,)).fetchone()[
            0
        ]
        if status in {"done", "cancelled", "attention"}:
            return
        self.start()
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(target, []).append(waiter)
        await asyncio.shield(waiter)

    def start(self):
        if self.scheduler is None:
            self.scheduler = asyncio.create_task(self.loop())

    async def restore(self):
        while not self.closed:
            try:
                await self.restore_once()
                return
            except errors.FloodWaitError as exc:
                await asyncio.sleep(exc.seconds + 1)
            except Exception:
                await asyncio.sleep(30)

    async def restore_once(self):
        row = self.db.execute("SELECT value FROM reply_meta WHERE key='online'").fetchone()
        if row:
            since = max(row[0] - 60, time.time() - 86400)
            try:
                async for dialog in self.bot.client.iter_dialogs():
                    if not dialog.is_user or dialog.date is None or dialog.date.timestamp() < since:
                        continue
                    found = []
                    async for message in self.bot.client.iter_messages(dialog.id, limit=None):
                        if message.date.timestamp() < since:
                            break
                        found.append(message)
                    for message in reversed(found):
                        event = restored_event(message, self.bot.client)
                        if await self.bot.eligible(event, restored=True):
                            self.enqueue(event, [message])
            except Exception as exc:
                LOG.warning("Догрузка сообщений отложена (%s).", type(exc).__name__)
                raise
        self.caught_up = True

    async def loop(self):
        while not self.closed:
            self.wakeup.clear()
            connected = getattr(self.bot.client, "is_connected", None)
            if connected:
                if not connected():
                    self.caught_up = False
                elif not self.caught_up and self.recovery and self.recovery.done():
                    self.recovery = asyncio.create_task(self.restore())
            if self.caught_up and time.time() - self.last_watermark >= 5:
                with self.db:
                    self.db.execute(
                        "INSERT OR REPLACE INTO reply_meta VALUES('online',?)", (time.time(),)
                    )
                self.last_watermark = time.time()
            rows = self.db.execute(
                "SELECT id,user_id,ids,epoch,data FROM reply_jobs WHERE status='pending' AND due<=? ORDER BY coalesce(json_extract(data,'$.control'),0) DESC,id",
                (time.time(),),
            ).fetchall()
            for row in rows:
                uid = row[1]
                if uid in self.runners or len(self.runners) >= 2:
                    continue
                # Never pass an older deferred turn in the same dialogue.
                if (
                    not json.loads(row[4]).get("control")
                    and self.db.execute(
                        "SELECT 1 FROM reply_jobs WHERE user_id=? AND id<? AND status IN ('pending','running')",
                        (uid, row[0]),
                    ).fetchone()
                ):
                    continue
                self.runners[uid] = asyncio.create_task(self.run(Turn(self, row)))
            try:
                await asyncio.wait_for(self.wakeup.wait(), 0.1 if self.waiters else 1)
            except TimeoutError:
                pass
            if not self.runners and not self.waiters and not self.recovery:
                self.scheduler = None
                return

    async def materialize(self, turn):
        messages, last_event = [], None
        missing = [mid for mid in turn.ids if (turn.user, mid) not in self.live]
        fetched = {}
        if missing:
            fetched = {
                m.id: m
                for m in await self.bot.client.get_messages(turn.user, ids=missing)
                if m and getattr(m, "message", None) is not None
            }
        for mid in turn.ids:
            if self.bot.history.changed(turn.user, mid):
                return None, []
            item = self.live.get((turn.user, mid))
            if item:
                last_event, message = item
            else:
                message = fetched.get(mid)
                if message is None:
                    return None, []
                last_event = restored_event(message, self.bot.client)
            messages.append(message)
        if not await self.bot.eligible(last_event, restored=True):
            return None, []
        if len(messages) == 1:
            return last_event, messages
        # Reply to the last message in a collected phrase, as Telegram's swipe reply does.
        return SimpleNamespace(
            sender_id=turn.user,
            id=last_event.id,
            raw_text="\n".join(m.message or "" for m in messages),
            message=messages[-1],
            reply=last_event.reply,
        ), messages

    async def run(self, turn):
        self.turns[turn.user] = turn
        if "input" not in turn.data and not turn.sealed:
            turn.epoch = self.bot.history.epoch(turn.user)
        with self.db:
            self.db.execute(
                "UPDATE reply_jobs SET status='running',epoch=?,attempts=attempts+1 WHERE id=?",
                (turn.epoch, turn.id),
            )
        token = CURRENT_TURN.set(turn)
        status, delay = "done", 0
        try:
            event, messages = await self.materialize(turn)
            if event is None or self.bot.history.epoch(turn.user) != turn.epoch:
                status = "cancelled"
            else:
                await self.bot.process(event, messages)
        except asyncio.CancelledError:
            status = "pending"
        except RetryTurn as exc:
            status, delay = "pending", exc.delay
        except errors.FloodWaitError as exc:
            status, delay = "pending", exc.seconds + 1
            self.bot.blocked_until = time.monotonic() + exc.seconds
        except DeliveryUncertain:
            status = "attention"
            LOG.warning("Отправка ответа %s требует проверки; повтор отключён.", turn.id)
        except (errors.RPCError, OSError, TimeoutError) as exc:
            status, delay = "pending", 15
            attempts = self.db.execute(
                "SELECT attempts FROM reply_jobs WHERE id=?", (turn.id,)
            ).fetchone()[0]
            if attempts >= 5:
                status = "attention"
            LOG.warning("Ответ %s отложен (%s).", turn.id, type(exc).__name__)
        except Exception as exc:
            status = "attention"
            LOG.error("Ответ %s требует проверки (%s).", turn.id, type(exc).__name__)
        finally:
            CURRENT_TURN.reset(token)
            if turn.id in self.merge_requested:
                self.merge_requested.discard(turn.id)
            with self.db:
                self.db.execute(
                    "UPDATE reply_jobs SET status=?,due=MAX(due,?),updated=? WHERE id=?",
                    (status, time.time() + delay, time.time(), turn.id),
                )
            if status in {"done", "cancelled", "attention"}:
                if status != "attention":
                    with self.db:
                        self.db.execute("UPDATE reply_jobs SET data='{}' WHERE id=?", (turn.id,))
                        self.db.execute("DELETE FROM reply_outbox WHERE job=?", (turn.id,))
                for mid in turn.ids:
                    self.bot.history.mark_seen(turn.user, mid)
                    self.live.pop((turn.user, mid), None)
                for waiter in self.waiters.pop(turn.id, []):
                    if not waiter.done():
                        waiter.set_result(status)
            self.turns.pop(turn.user, None)
            self.runners.pop(turn.user, None)
            self.wakeup.set()

    def report(self):
        labels = {
            "pending": "Ожидает",
            "running": "Обрабатывается",
            "attention": "Нужна проверка отправки",
        }
        rows = self.db.execute(
            "SELECT id,user_id,ids,status,due FROM reply_jobs WHERE status IN ('pending','running','attention') ORDER BY id LIMIT 20"
        ).fetchall()
        if not rows:
            return "Очередь ответов пуста."
        return "Очередь ответов:\n" + "\n".join(
            f"#{r[0]}, диалог {r[1]}, сообщений {len(json.loads(r[2]))}: {labels[r[3]]}"
            for r in rows
        )

    async def close(self):
        self.closed = True
        tasks = [t for t in (self.scheduler, self.recovery, *self.runners.values()) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for waiters in self.waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
