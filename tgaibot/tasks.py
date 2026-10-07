"""Durable background work, isolated per correspondent and recoverable after restart."""

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from telethon import errors

from .agent import TOOLS, tool, with_attribution
from .artifacts import bounded_text
from .formatting import formatted_chunks

LOG = logging.getLogger(__name__)
LABELS = {
    "queued": "В очереди",
    "running": "Выполняется",
    "completed": "Готово",
    "needs_input": "Нужно уточнение",
    "attention": "Нужна проверка отправки",
    "failed": "Не удалось завершить",
    "cancelled": "Отменена",
}
ACTION_LABELS = {
    "search_web": "Поиск в интернете",
    "search_places": "Поиск на карте",
    "send_location": "Отправка геолокации",
    "create_pdf": "Создание PDF",
    "create_excel": "Создание Excel",
    "create_text_file": "Создание файла",
    "task_plan": "План работы",
}
READ_TOOLS = {"search_web", "search_places", "task_plan"}
BACKGROUND_TOOLS = [
    *TOOLS,
    tool(
        "task_plan",
        "Сохранить короткий план работы перед выполнением задачи. Это намерения, не выполненные действия.",
        {"steps": {"type": "array", "items": {"type": "string"}}},
        ["steps"],
    ),
    tool(
        "finish_task",
        "Завершить задачу с итогом или запросить недостающие сведения. Вызывай отдельно от остальных инструментов. completed только если просьба выполнена; иначе needs_input.",
        {
            "status": {"type": "string", "enum": ["completed", "needs_input"]},
            "summary": {"type": "string"},
        },
        ["status", "summary"],
    ),
]
BG_SCHEMAS = {t["function"]["name"]: t["function"]["parameters"] for t in BACKGROUND_TOOLS}


class TaskStore:
    def __init__(self, settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.data_dir / "tasks.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
                fingerprint TEXT NOT NULL, title TEXT NOT NULL, instruction TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                state TEXT NOT NULL, plan TEXT NOT NULL DEFAULT '[]', progress TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '', steps INTEGER NOT NULL DEFAULT 0,
                notification TEXT NOT NULL DEFAULT 'none', UNIQUE(user_id,source_id,fingerprint));
            CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status,due);
            CREATE TABLE IF NOT EXISTS task_actions (
                task_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(task_id,fingerprint));
        """)
        with self.db:
            self.db.execute(
                "UPDATE tasks SET status='queued',progress='Продолжу с сохранённого шага' WHERE status='running'"
            )
            self.db.execute("""UPDATE tasks SET status='attention',progress='Отправка могла произойти перед перезапуском',
                result='Не могу подтвердить доставку после перезапуска. Проверь сообщения выше; повторная отправка автоматически не выполняется.',
                notification='pending' WHERE status='queued' AND id IN
                (SELECT task_id FROM task_actions WHERE status='started' AND name NOT IN ('search_web','search_places','task_plan'))""")
            self.db.execute(
                "UPDATE tasks SET notification='uncertain' WHERE notification='sending'"
            )

    def close(self):
        self.db.close()

    def get(self, user_id, task_id):
        if not isinstance(task_id, int) or isinstance(task_id, bool):
            raise ValueError("Укажи номер задачи.")
        row = self.db.execute(
            "SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, user_id)
        ).fetchone()
        if not row:
            raise ValueError("Такой задачи в этом диалоге нет.")
        return row

    def create(self, user_id, source_id, title, instruction, context, delay_minutes=0):
        bounded_text(title, 120)
        bounded_text(instruction, 8000)
        if (
            isinstance(delay_minutes, bool)
            or not isinstance(delay_minutes, int)
            or not 0 <= delay_minutes <= 10080
        ):
            raise ValueError("Отложить задачу можно на 0–10080 минут.")
        fingerprint = hashlib.sha256(
            json.dumps([title, instruction, delay_minutes], ensure_ascii=False).encode()
        ).hexdigest()
        old = self.db.execute(
            "SELECT id FROM tasks WHERE user_id=? AND source_id=? AND fingerprint=?",
            (user_id, source_id, fingerprint),
        ).fetchone()
        if old:
            return old[0]
        active = self.db.execute(
            "SELECT count(*) FROM tasks WHERE user_id=? AND status IN ('queued','running')",
            (user_id,),
        ).fetchone()[0]
        total = self.db.execute(
            "SELECT count(*) FROM tasks WHERE status IN ('queued','running')"
        ).fetchone()[0]
        daily = self.db.execute(
            "SELECT count(*) FROM tasks WHERE user_id=? AND created>?",
            (user_id, time.time() - 86400),
        ).fetchone()[0]
        if active >= 3 or total >= 20 or daily >= 15:
            raise ValueError("Сейчас слишком много задач. Дождись завершения или отмени ненужную.")
        state = {
            "conversation": [
                *context,
                {"role": "user", "content": "Фоновое поручение: " + instruction},
            ],
            "pending": None,
            "index": 0,
            "places": {},
        }
        raw = json.dumps(state, ensure_ascii=False)
        if len(raw.encode()) > 8_000_000:
            raise ValueError(
                "Для фоновой задачи слишком много вложений. Отправь её отдельным сообщением."
            )
        now = time.time()
        with self.db:
            row = self.db.execute(
                """INSERT INTO tasks(user_id,source_id,fingerprint,title,instruction,status,
                created,updated,due,state,progress) VALUES(?,?,?,?,?,'queued',?,?,?,?,?)""",
                (
                    user_id,
                    source_id,
                    fingerprint,
                    title,
                    instruction,
                    now,
                    now,
                    now + delay_minutes * 60,
                    raw,
                    "Ожидает запуска",
                ),
            )
        return row.lastrowid

    def update(self, task_id, **values):
        allowed = {"status", "due", "state", "plan", "progress", "result", "steps", "notification"}
        if not set(values) <= allowed:
            raise ValueError("Invalid task fields")
        values["updated"] = time.time()
        with self.db:
            self.db.execute(
                "UPDATE tasks SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                (*values.values(), task_id),
            )

    def save_state(self, task_id, state):
        self.update(task_id, state=json.dumps(state, ensure_ascii=False))

    def action(self, task_id, key):
        return self.db.execute(
            "SELECT * FROM task_actions WHERE task_id=? AND fingerprint=?", (task_id, key)
        ).fetchone()

    def start_action(self, task_id, key, name):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO task_actions VALUES(?,?,?,'started',NULL,?,?)",
                (task_id, key, name, time.time(), time.time()),
            )

    def finish_action(self, task_id, key, result):
        with self.db:
            self.db.execute(
                "UPDATE task_actions SET status=?,result=?,updated=? WHERE task_id=? AND fingerprint=?",
                (
                    "error" if "error" in result else "done",
                    json.dumps(result, ensure_ascii=False),
                    time.time(),
                    task_id,
                    key,
                ),
            )

    def describe(self, user_id, task_id):
        row = self.get(user_id, task_id)
        actions = self.db.execute(
            "SELECT name,status,result FROM task_actions WHERE task_id=? ORDER BY created",
            (task_id,),
        ).fetchall()
        return {
            "id": row["id"],
            "title": row["title"],
            "status": LABELS[row["status"]],
            "progress": row["progress"],
            "plan": json.loads(row["plan"]),
            "result": row["result"],
            "due_unix": row["due"],
            "notification": row["notification"],
            "actions": [
                {
                    "action": ACTION_LABELS.get(a["name"], a["name"]),
                    "status": a["status"],
                    "result": json.loads(a["result"]) if a["result"] else None,
                }
                for a in actions
            ],
        }

    def list(self, user_id):
        rows = self.db.execute(
            "SELECT id,title,status,progress,result,due FROM tasks WHERE user_id=? ORDER BY id DESC LIMIT 12",
            (user_id,),
        ).fetchall()
        return [{**dict(r), "status": LABELS[r["status"]]} for r in rows]

    def forget(self, user_id):
        with self.db:
            self.db.execute(
                "DELETE FROM task_actions WHERE task_id IN (SELECT id FROM tasks WHERE user_id=?)",
                (user_id,),
            )
            self.db.execute("DELETE FROM tasks WHERE user_id=?", (user_id,))


class TaskManager:
    def __init__(self, settings, agent, history):
        self.settings, self.agent, self.history = settings, agent, history
        self.store = TaskStore(settings)
        self.loop_task = None
        self.active_task = None
        self.active_id = None
        self.delivery_task = None
        self.delivery_user = None
        self.wakeup = asyncio.Event()
        self.closing = False
        self.blocked_users = set()

    def start(self):
        self.loop_task = asyncio.create_task(self.worker())

    def create(self, user_id, source_id, context, **args):
        task_id = self.store.create(user_id, source_id, context=context, **args)
        self.wakeup.set()
        return {
            "created": True,
            "task_id": task_id,
            "status": "В очереди",
            "message": "Задача сохранена; результат придёт в этот чат.",
        }

    async def cancel(self, user_id, task_id):
        row = self.store.get(user_id, task_id)
        if row["status"] in {"completed", "cancelled"}:
            return {"status": LABELS[row["status"]]}
        self.store.update(
            task_id, status="cancelled", progress="Остановлена пользователем", notification="none"
        )
        if self.active_id == task_id and self.active_task:
            self.active_task.cancel()
            await asyncio.gather(self.active_task, return_exceptions=True)
        return {"status": "Отменена", "note": "Уже отправленные файлы и сообщения остаются в чате."}

    async def suspend(self, user_id, forget=False):
        self.blocked_users.add(user_id)
        if self.delivery_user == user_id and self.delivery_task:
            self.delivery_task.cancel()
            await asyncio.gather(self.delivery_task, return_exceptions=True)
        if self.active_id:
            row = self.store.db.execute(
                "SELECT user_id FROM tasks WHERE id=?", (self.active_id,)
            ).fetchone()
            if row and row[0] == user_id and self.active_task:
                self.active_task.cancel()
                await asyncio.gather(self.active_task, return_exceptions=True)
        if forget:
            self.store.forget(user_id)
        self.blocked_users.discard(user_id)

    def resume(self, user_id, task_id, instruction):
        row = self.store.get(user_id, task_id)
        bounded_text(instruction, 4000)
        if row["status"] != "needs_input":
            raise ValueError(
                "Продолжить можно задачу, которая ждёт уточнения. Для отменённой или непроверенной отправки создай новое поручение."
            )
        if row["steps"] >= 16:
            raise ValueError(
                "Лимит шагов этой задачи исчерпан. Создай отдельную задачу на оставшуюся часть."
            )
        active = self.store.db.execute(
            "SELECT count(*) FROM tasks WHERE user_id=? AND status IN ('queued','running')",
            (user_id,),
        ).fetchone()[0]
        total = self.store.db.execute(
            "SELECT count(*) FROM tasks WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if active >= 3 or total >= 20:
            raise ValueError("Очередь занята. Дождись завершения одной из текущих задач.")
        state = json.loads(row["state"])
        state["conversation"].append({"role": "user", "content": instruction})
        self.store.update(
            task_id,
            state=json.dumps(state, ensure_ascii=False),
            status="queued",
            result="",
            due=time.time(),
            notification="none",
            progress="Получено уточнение",
        )
        self.wakeup.set()
        return {"task_id": task_id, "status": "В очереди"}

    async def worker(self):
        while not self.closing:
            try:
                # Notifications are separate from execution, so a model error cannot erase results.
                for row in self.store.db.execute(
                    "SELECT * FROM tasks WHERE notification='pending' ORDER BY id LIMIT 20"
                ).fetchall():
                    if (
                        not self.history.is_paused(row["user_id"])
                        and row["user_id"] not in self.blocked_users
                    ):
                        self.delivery_user = row["user_id"]
                        self.delivery_task = asyncio.create_task(self.notify(row))
                        await asyncio.gather(self.delivery_task, return_exceptions=True)
                        self.delivery_user, self.delivery_task = None, None
                rows = self.store.db.execute(
                    "SELECT * FROM tasks WHERE status='queued' AND due<=? ORDER BY due,id LIMIT 20",
                    (time.time(),),
                ).fetchall()
                row = next(
                    (
                        r
                        for r in rows
                        if not self.history.is_paused(r["user_id"])
                        and r["user_id"] not in self.blocked_users
                    ),
                    None,
                )
                if row:
                    self.active_id = row["id"]
                    self.active_task = asyncio.create_task(self.run_task(row["user_id"], row["id"]))
                    await asyncio.gather(self.active_task, return_exceptions=True)
                    self.active_id, self.active_task = None, None
                    continue
                self.wakeup.clear()
                try:
                    await asyncio.wait_for(self.wakeup.wait(), 2)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Фоновые задачи: %s", type(exc).__name__)
                await asyncio.sleep(2)

    async def run_task(self, user_id, task_id):
        self.store.update(task_id, status="running", progress="Продумываю задачу")
        try:
            async with asyncio.timeout(1200):
                await self.execute_steps(user_id, task_id)
            if self.store.get(user_id, task_id)["status"] == "running":
                self.store.update(
                    task_id, status="queued", progress="Приостановлена; продолжу позже"
                )
        except asyncio.CancelledError:
            row = self.store.get(user_id, task_id)
            if row["status"] == "running":
                self.store.update(
                    task_id, status="queued", progress="Приостановлена; продолжу позже"
                )
            raise
        except Exception as exc:
            LOG.warning("Задача #%s остановлена (%s)", task_id, type(exc).__name__)
            self.store.update(
                task_id,
                status="failed",
                result="Не удалось завершить задачу. Сделанные шаги сохранены; спроси о ходе задачи.",
                progress="Работа остановлена",
                notification="pending",
            )

    async def execute_steps(self, user_id, task_id):
        row = self.store.get(user_id, task_id)
        state = json.loads(row["state"])

        def valid():
            return (
                self.store.get(user_id, task_id)["status"] == "running"
                and not self.history.is_paused(user_id)
                and user_id not in self.blocked_users
            )

        while valid():
            row = self.store.get(user_id, task_id)
            if not state["pending"]:
                if row["steps"] >= 16:
                    self.store.update(
                        task_id,
                        status="needs_input",
                        result="Достигнут лимит работы. Сделанные шаги сохранены; оставшуюся часть можно поручить отдельно.",
                        notification="pending",
                        progress="Достигнут лимит шагов",
                    )
                    return
                self.store.update(
                    task_id, steps=row["steps"] + 1, progress="Обдумываю следующий шаг"
                )
                instruction = {
                    "role": "system",
                    "content": "Ты выполняешь сохранённую фоновую задачу. Сначала сохрани короткий план через task_plan, затем выполняй его доступными инструментами. Не создавай новые фоновые задачи. Действия только в этом чате. План не означает выполнение. Уточнения запрашивай через finish_task(status=needs_input). По завершении проверь результаты инструментов и вызови finish_task(status=completed, summary=конкретный итог со ссылками). Этот инструмент вызывается отдельно. Если не хватает возможностей, честно сообщи об этом. Не обещай будущую работу после завершения.",
                }
                response = await self.agent.provider.step(
                    [instruction, *state["conversation"]], BACKGROUND_TOOLS
                )
                if not valid():
                    return
                state["conversation"].append(response)
                calls = response.get("tool_calls") or []
                if not calls:
                    self.store.save_state(task_id, state)
                    self.store.update(
                        task_id,
                        status="needs_input",
                        result=response["content"],
                        notification="pending",
                        progress="Ожидаю уточнение",
                    )
                    return
                state["pending"], state["index"] = calls, 0
                self.store.save_state(task_id, state)
            while state["pending"] and state["index"] < len(state["pending"]) and valid():
                call = state["pending"][state["index"]]
                name = call["function"]["name"]
                raw = call["function"]["arguments"]
                if not isinstance(raw, str) or len(raw) > 80000:
                    raise ValueError("Invalid arguments")
                args = json.loads(raw)
                schema = BG_SCHEMAS.get(name)
                if not schema or not isinstance(args, dict) or set(args) != set(schema["required"]):
                    raise ValueError("Unknown task action")
                key = hashlib.sha256(
                    json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                previous = self.store.action(task_id, key)
                if name == "finish_task":
                    if len(state["pending"]) != 1 or args["status"] not in {
                        "completed",
                        "needs_input",
                    }:
                        raise ValueError("Invalid finish")
                    bounded_text(args["summary"], 18000)
                    state["conversation"].append(
                        {"role": "tool", "tool_call_id": call["id"], "content": "Итог сохранён."}
                    )
                    state["pending"], state["index"] = None, 0
                    self.store.update(
                        task_id,
                        state=json.dumps(state, ensure_ascii=False),
                        status=args["status"],
                        result=with_attribution(args["summary"], state["places"]),
                        progress="Работа завершена"
                        if args["status"] == "completed"
                        else "Ожидаю уточнение",
                        notification="pending",
                    )
                    return
                if previous and previous["status"] in {"done", "error"}:
                    result = json.loads(previous["result"])
                    if name == "search_places":
                        for place in result.get("places", []):
                            state["places"][place["place_id"]] = place
                elif previous and name not in READ_TOOLS:
                    self.store.update(
                        task_id,
                        status="attention",
                        result="Отправка могла произойти, но подтверждение не сохранилось. Проверь чат; автоматически повторять её не буду.",
                        notification="pending",
                        progress="Проверь отправку",
                    )
                    return
                else:
                    if (
                        self.store.db.execute(
                            "SELECT count(*) FROM task_actions WHERE task_id=?", (task_id,)
                        ).fetchone()[0]
                        >= 32
                    ):
                        raise ValueError("Task action budget exhausted")
                    self.store.start_action(task_id, key, name)
                    self.store.update(task_id, progress=ACTION_LABELS.get(name, "Выполняю шаг"))
                    try:
                        if name == "task_plan":
                            steps = args["steps"]
                            if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
                                raise ValueError("В плане нужно 1–12 шагов.")
                            for step in steps:
                                bounded_text(step, 300)
                            self.store.update(task_id, plan=json.dumps(steps, ensure_ascii=False))
                            result = {"plan_saved": True}
                        else:
                            result = await self.agent.execute(
                                name,
                                args,
                                self.settings.data_dir / "attachments" / str(user_id),
                                user_id,
                                row["source_id"],
                                state["places"],
                                valid,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if name not in READ_TOOLS:
                            self.store.update(
                                task_id,
                                status="attention",
                                result="Не удалось подтвердить отправку. Проверь чат; повторная отправка автоматически не выполняется.",
                                notification="pending",
                                progress="Проверь отправку",
                            )
                            return
                        result = {
                            "error": "Шаг не выполнен: сервис недоступен или параметры неверны."
                        }
                        LOG.warning("Шаг задачи #%s: %s", task_id, type(exc).__name__)
                    self.store.finish_action(task_id, key, result)
                state["conversation"].append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                state["index"] += 1
                self.store.save_state(task_id, state)
            if not valid():
                return
            state["pending"], state["index"] = None, 0
            self.store.save_state(task_id, state)

    async def notify(self, row):
        current = self.store.get(row["user_id"], row["id"])
        if (
            current["notification"] != "pending"
            or current["status"] != row["status"]
            or self.history.is_paused(row["user_id"])
            or row["user_id"] in self.blocked_users
        ):
            return
        self.store.update(row["id"], notification="sending")
        try:
            text = (
                f"Задача #{row['id']} · {row['title']}\n{LABELS[row['status']]}\n\n{row['result']}"
            )
            for part, entities in formatted_chunks(text):
                if (
                    self.history.is_paused(row["user_id"])
                    or row["user_id"] in self.blocked_users
                    or self.store.get(row["user_id"], row["id"])["status"] != row["status"]
                ):
                    self.store.update(row["id"], notification="uncertain")
                    return
                await self.agent.client.send_message(
                    row["user_id"],
                    part,
                    formatting_entities=entities,
                    parse_mode=None,
                    link_preview=False,
                )
            self.store.update(row["id"], notification="sent")
            # Unique negative IDs keep task outcomes separate from original Telegram message pairs.
            self.history.add(
                row["user_id"], -row["id"], "Итог фоновой задачи: " + row["title"], row["result"]
            )
        except asyncio.CancelledError:
            self.store.update(row["id"], notification="uncertain")
            raise
        except Exception as exc:
            self.store.update(row["id"], notification="uncertain")
            LOG.warning("Уведомление задачи #%s: %s", row["id"], type(exc).__name__)
            if isinstance(exc, errors.FloodWaitError):
                await asyncio.sleep(exc.seconds)

    def report(self, user_id, task_id=None):
        if task_id is None:
            rows = self.store.list(user_id)
            return (
                "Фоновых задач пока нет. Напиши, например: «В фоне сравни варианты поездки и сделай PDF»."
                if not rows
                else "Твои задачи:\n\n"
                + "\n\n".join(
                    f"#{r['id']} · {r['title']}\n{r['status']} — {r['progress']}"
                    + ("\n" + r["result"][:300] if r["result"] else "")
                    + (
                        "\nНачну: "
                        + datetime.fromtimestamp(r["due"], timezone(timedelta(hours=5))).strftime(
                            "%d.%m %H:%M (UTC+05:00)"
                        )
                        if r["status"] == "В очереди" and r["due"] > time.time() + 30
                        else ""
                    )
                    for r in rows
                )
            )
        data = self.store.describe(user_id, task_id)
        lines = [f"Задача #{task_id} · {data['title']}", data["status"] + " — " + data["progress"]]
        if data["plan"]:
            lines += ["\nПлан:", *[f"{i + 1}. {s}" for i, s in enumerate(data["plan"])]]
        done = [
            a for a in data["actions"] if a["status"] == "done" and a["action"] != "План работы"
        ]
        if done:
            lines += [
                "\nВыполнено:",
                *[
                    "• "
                    + a["action"]
                    + (": " + a["result"]["sent"] if a["result"].get("sent") else "")
                    for a in done
                ],
            ]
        if data["result"]:
            lines += ["\nИтог:", data["result"]]
        if data["notification"] == "uncertain":
            lines += ["\nАвтоуведомление могло не дойти; результат сохранён здесь."]
        return "\n".join(lines)

    async def close(self):
        self.closing = True
        for task in (self.active_task, self.delivery_task, self.loop_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *[t for t in (self.active_task, self.delivery_task, self.loop_task) if t],
            return_exceptions=True,
        )
        self.store.close()
