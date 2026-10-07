"""Persistent operational counters, without prompts, credentials or message contents."""

import sqlite3
import time
from datetime import datetime, timedelta, timezone


def number(value):
    return value if type(value) is int and 0 <= value < 2**60 else None


def normalize_usage(usage):
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    details = details if isinstance(details, dict) else {}
    cached = number(details.get("cached_tokens", usage.get("cache_read_input_tokens")))
    written = number(details.get("cache_write_tokens", usage.get("cache_creation_input_tokens")))
    prompt = number(usage.get("prompt_tokens", usage.get("input_tokens")))
    output = number(usage.get("completion_tokens", usage.get("output_tokens")))
    # Native Anthropic excludes cache tokens from input_tokens. OpenAI includes them.
    if (
        "prompt_tokens" not in usage
        and "input_tokens" in usage
        and ("cache_read_input_tokens" in usage or "cache_creation_input_tokens" in usage)
    ):
        prompt = prompt + (cached or 0) + (written or 0) if prompt is not None else None
    return prompt, output, cached, written


class Metrics:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "metrics.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL,
            ok INTEGER NOT NULL, amount INTEGER NOT NULL, model TEXT NOT NULL,
            input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
            cache_write_tokens INTEGER, input_chars INTEGER, output_chars INTEGER)""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        for name in ("cost_nano", "cost_complete", "estimate_nano"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {name} INTEGER")
        self.db.execute("CREATE INDEX IF NOT EXISTS event_time ON events(created)")
        self.db.commit()

    def close(self):
        self.db.close()

    def record(
        self, kind, *, usage=None, ok=True, amount=1, model="", input_chars=None, output_chars=None
    ):
        tokens = normalize_usage(usage)
        prompt, output, cached, written = tokens
        cost, complete = None, False
        estimate = None
        if kind in {"model", "summary", "background"} and model == "claude-sonnet-5-5":
            # USD per million: input .36, output 1.8, cache read .036, write .45.
            # Integer nanodollars avoid floating-point rounding of tiny charges.
            cost = output * 1800 if output is not None else 0
            if prompt is not None and output is not None:
                estimate = (
                    cost
                    + max(0, prompt - (cached or 0) - (written or 0)) * 360
                    + (cached or 0) * 36
                    + (written or 0) * 450
                )
            if all(v is not None for v in (prompt, cached, written)) and cached + written <= prompt:
                cost += (prompt - cached - written) * 360 + cached * 36 + written * 450
                complete = output is not None
        elif kind == "image" and ok:
            cost, complete = amount * 1_200_000, True
            estimate = cost
        with self.db:
            self.db.execute(
                "INSERT INTO events VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    kind,
                    int(ok),
                    amount,
                    model,
                    *tokens,
                    input_chars,
                    output_chars,
                    cost,
                    int(complete),
                    estimate,
                ),
            )

    def report(self, *, all_time=False):
        local = datetime.now(timezone(timedelta(hours=5)))
        since = (
            0 if all_time else local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        rows = self.db.execute(
            """SELECT kind, count(*) AS requests, sum(ok) AS successes,
            sum(CASE WHEN ok THEN amount ELSE 0 END) AS amount
            FROM events WHERE created>=? GROUP BY kind""",
            (since,),
        ).fetchall()
        counts = {row["kind"]: dict(row) for row in rows}
        row = self.db.execute(
            """SELECT count(*) AS requests,
            count(input_tokens) AS input_known, sum(input_tokens) AS input_tokens,
            count(output_tokens) AS output_known, sum(output_tokens) AS output_tokens,
            count(cached_tokens) AS cached_known, sum(cached_tokens) AS cached_tokens,
            count(cache_write_tokens) AS written_known, sum(cache_write_tokens) AS written_tokens
            FROM events WHERE created>=? AND kind IN ('model','summary','background')""",
            (since,),
        ).fetchone()

        def tokens(key, known):
            n = row[known]
            if not n:
                return "нет данных"
            suffix = f" (данные {n}/{row['requests']} запросов)" if n < row["requests"] else ""
            return f"{row[key]:,}".replace(",", " ") + suffix

        def count(kind):
            return counts.get(kind, {}).get("amount", 0)

        first = self.db.execute("SELECT min(created) FROM events").fetchone()[0]
        started = (
            datetime.fromtimestamp(first, local.tzinfo).strftime("%d.%m.%Y %H:%M")
            if first
            else "пока нет событий"
        )
        total = row["requests"]
        errors = sum(c["requests"] - c["successes"] for c in counts.values())
        cost_rows = self.db.execute(
            """SELECT kind,coalesce(sum(cost_nano),0) AS cost,
            sum(CASE WHEN cost_complete=1 THEN 0 ELSE 1 END) AS unknown
            FROM events WHERE created>=? AND kind IN ('model','summary','background','image','voice')
            GROUP BY kind""",
            (since,),
        ).fetchall()
        costs = {r["kind"]: r["cost"] for r in cost_rows}
        unknown = sum(r["unknown"] for r in cost_rows)
        estimate = self.db.execute(
            "SELECT coalesce(sum(estimate_nano),0) FROM events WHERE created>=?", (since,)
        ).fetchone()[0]
        model_cost = sum(costs.get(k, 0) for k in ("model", "summary", "background"))
        image_cost = costs.get("image", 0)

        def money(n):
            return f"${n / 1_000_000_000:.8f}"

        return (
            f"Статистика {'за всё время' if all_time else 'за сегодня (UTC+05:00)'}\n\n"
            f"Запросов модели: {total}\n"
            f"Из них для сжатия: {counts.get('summary', {}).get('requests', 0)}\n"
            f"Из них фоновых: {counts.get('background', {}).get('requests', 0)}\n"
            f"Входных токенов: {tokens('input_tokens', 'input_known')}\n"
            f"Выходных токенов: {tokens('output_tokens', 'output_known')}\n"
            f"Прочитано из кэша: {tokens('cached_tokens', 'cached_known')}\n"
            f"Записано в кэш: {tokens('written_tokens', 'written_known')}\n\n"
            f"Изображений сгенерировано: {count('image')}\n"
            f"Озвучек создано: {count('voice')}\n"
            f"Файлов отправлено: {count('file')}\n"
            f"Геолокаций отправлено: {count('location')}\n"
            f"Поисков выполнено: {count('search')}\n"
            f"Сжатий памяти сохранено: {count('compression')}\n"
            f"Ошибок учтённых API-запросов: {errors}\n\n"
            f"Расход Claude (известная часть): {money(model_cost)}\n"
            f"Расход на изображения: {money(image_cost)}\n"
            f"Итого по известным данным: {money(model_cost + image_cost)}\n"
            f"Оценка с входными токенами: {money(estimate)}\n"
            "Для оценки отсутствующие данные чтения/записи кэша приняты за 0. Неизвестные токены и озвучка не включены.\n"
            f"Событий с неполной стоимостью: {unknown}\n\n"
            "Тарифы за 1 млн токенов: вход $0.36, выход $1.80, чтение кэша $0.036, запись $0.45.\n"
            "Изображение: $0.0012. Тариф озвучки не задан. Это расчёт, не счёт провайдера.\n"
            f"Учёт начат: {started}. Старые расходы не восстановлены.\n"
            "Токены взяты из ответа API. Кэш уже входит во входные токены."
        )


def memory_report(history, settings):
    db = history.db
    turns, users = db.execute("SELECT count(*),count(DISTINCT user_id) FROM turns").fetchone()
    summaries, chars = db.execute(
        "SELECT count(*),coalesce(sum(length(content)),0) FROM summaries"
    ).fetchone()
    return (
        f"Память ИИ\n\nДиалогов: {users}\nСохранённых пар сообщений: {turns}\n"
        f"Резюме диалогов: {summaries}\nОбщий размер резюме: {chars} символов\n\n"
        f"Свежий контекст: до {settings.history_turns} пар и {settings.history_chars} символов\n"
        f"Резюме одного диалога: до {settings.summary_chars} символов\n"
        "Старые детали подбираются поиском по архиву отдельно для каждого собеседника. "
        "Сжатие не удаляет оригиналы. Ограничения в символах, не в токенах."
    )
