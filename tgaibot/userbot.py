import asyncio
import logging
import re
import sys
import tempfile
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from types import SimpleNamespace

from telethon import TelegramClient, errors, events, functions
from telethon.sessions import StringSession

from .agent import Agent
from .archive import Archive, Archiver
from .conversation import background_request, control_intent, simple_error, task_control
from .formatting import formatted_chunks, plain_fallback
from .images import IMAGE_MODEL, ImageGenerator, image_prompt
from .media import SUPPORTED_EXT, MediaError, Prepared, image_data
from .memory import Memory
from .provider import Provider, ProviderError
from .search import Search
from .storage import History, image_count, text_size
from .tts import FishTTS, TTSError
from .turns import CURRENT_TURN, SEND_TYPES, RetryTurn, TurnQueue
from .worker import prepare_isolated

LOG = logging.getLogger(__name__)
HELP = (
    "Просто пиши мне, как в обычной переписке. Я ИИ-помощник: могу объяснить непонятное, "
    "обсудить идею, помочь с текстом или разобраться в фото, документе и коротком видео.\n\n"
    "Можно прямо так:\n"
    "• «Отвечай голосом» — буду присылать голосовые и текст.\n"
    "• «Пиши текстом» — без голосовых.\n"
    "• «Нарисуй котика на подоконнике» — создам картинку.\n"
    "• «Расшифруй это аудио» — пришли голосовое или аудиофайл.\n"
    "• «Сделай PDF с планом поездки» или «Сделай Excel с расходами» — пришлю файл.\n"
    "• «Найди в интернете…» — поищу и дам ссылки.\n"
    "• «Найди кафе в Алматы и пришли точку» — поищу на карте.\n"
    "• «В фоне сравни варианты поездки и сделай PDF» — займусь задачей отдельно от переписки.\n"
    "• «Что сделано?», «Статус задачи 3», «Отмени задачу 3» — управление фоновыми поручениями.\n"
    "• «Вспомни, что мы решили про поездку» — поищу в нашей переписке.\n"
    "• «Покажи память» — покажу сохранённые сведения; «Забудь мой возраст» — уберу это поле.\n"
    "• «Забудь нашу переписку» — очищу память ИИ; сообщения в Telegram и личном архиве владельца останутся.\n"
    "• «Не отвечай мне» — остановлюсь; «Давай продолжим» — снова буду отвечать.\n\n"
    "Фото и файлы — до 20 МБ, видео и голосовые — до трёх минут. "
    "Видео вижу по отдельным кадрам, поэтому могу пропустить быстрые события. "
    "Если файл не подойдёт, объясню, как лучше его прислать. Звонки пока в тестовом режиме.\n\n"
    "Память помогает продолжать разговор, но иногда важную деталь нужно напомнить. "
    "Переписка сохраняется в хранилище помощника до очистки. Текст и подготовленные вложения "
    "обрабатываются внешним ИИ-сервисом; для голоса текст ответа передаётся Fish Audio."
)


def chunks(text, limit=3500):
    """Telegram measures UTF-16 units; also avoid splitting astral characters."""
    current, size = [], 0
    for char in text:
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > limit:
            yield "".join(current)
            current, size = [], 0
        current.append(char)
        size += units
    if current:
        yield "".join(current)


class IncomingOnlyTelegramClient(TelegramClient):
    # PyTgCalls 3.0 detects its adapter by class.__module__, not isinstance/MRO.
    # Preserve Telethon's package marker while keeping our outbound RPC guard.
    __module__ = TelegramClient.__module__
    archive_sink = None

    async def record_sent(self, result):
        if self.archive_sink:
            for message in result if isinstance(result, list) else [result]:
                if message and getattr(message, "is_private", False):
                    await self.archive_sink(
                        SimpleNamespace(
                            is_private=True,
                            chat_id=message.chat_id,
                            get_chat=message.get_chat,
                            message=message,
                        )
                    )
        return result

    async def send_message(self, *args, **kwargs):
        return await self.record_sent(await super().send_message(*args, **kwargs))

    async def send_file(self, *args, **kwargs):
        return await self.record_sent(await super().send_file(*args, **kwargs))

    async def __call__(self, request, *args, **kwargs):
        requests = request if isinstance(request, (list, tuple)) else [request]
        if any(isinstance(item, functions.phone.RequestCallRequest) for item in requests):
            raise RuntimeError("Исходящие звонки запрещены: принимаются только входящие.")
        turn = CURRENT_TURN.get()
        if turn and isinstance(request, SEND_TYPES):
            return await turn.rpc(
                request,
                lambda saved: super(IncomingOnlyTelegramClient, self).__call__(
                    saved, *args, **kwargs
                ),
            )
        return await super().__call__(request, *args, **kwargs)


def make_client(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return IncomingOnlyTelegramClient(
        StringSession(settings.telegram_session)
        if settings.telegram_session
        else str(settings.data_dir / "telegram"),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        flood_sleep_threshold=0,
        request_retries=1,
        connection_retries=5,
        catch_up=False,
    )


@contextmanager
def account_lock(directory):
    """An OS lock prevents two local processes using one MTProto session."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "userbot.lock").open("a+b") as handle:
        handle.seek(0)
        if (directory / "userbot.lock").stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Эта сессия уже запущена другим процессом. Сначала остановите его.")
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def login(settings):
    if not sys.stdin.isatty():
        raise RuntimeError("Запустите login в интерактивном терминале: ввод номера/кода/2FA скрыт.")
    client = make_client(settings)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            if me.bot:
                raise RuntimeError("Нужна сессия обычного Telegram-аккаунта, не бота.")
            print("Аккаунт уже авторизован. Сессия готова.")
            return
        phone = getpass("Номер Telegram (+код страны, ввод скрыт): ").strip()
        if not phone.startswith("+") or not phone[1:].isdigit():
            raise RuntimeError("Номер должен начинаться с + и содержать только цифры.")
        sent = await client.send_code_request(phone)
        for attempt in range(3):
            try:
                await client.sign_in(
                    phone=phone,
                    code=getpass("Код Telegram (ввод скрыт): ").strip(),
                    phone_code_hash=sent.phone_code_hash,
                )
                break
            except errors.PhoneCodeInvalidError:
                if attempt == 2:
                    raise RuntimeError("Код не принят. Повторите login позже.")
                print("Код не принят; попробуйте ещё раз.")
            except errors.SessionPasswordNeededError:
                for password_attempt in range(3):
                    try:
                        await client.sign_in(password=getpass("Пароль 2FA (ввод скрыт): "))
                        break
                    except errors.PasswordHashInvalidError:
                        if password_attempt == 2:
                            raise RuntimeError("Пароль 2FA не принят.")
                        print("Пароль не принят; попробуйте ещё раз.")
                break
        me = await client.get_me()
        if not me or me.bot:
            raise RuntimeError("Не удалось авторизовать обычный Telegram-аккаунт.")
        print("Авторизация завершена. Сессия сохранена локально. Запустите python -m tgaibot run")
    finally:
        await client.disconnect()


class Userbot:
    def __init__(
        self, settings, client, provider, history, self_id, tts=None, images=None, agent=None
    ):
        self.settings, self.client, self.provider, self.history = (
            settings,
            client,
            provider,
            history,
        )
        self.self_id = self_id
        self.started = datetime.now(timezone.utc).replace(microsecond=0)
        self.slots = asyncio.Semaphore(2)
        self.blocked_until = 0.0
        self.tts = tts
        self.memory = Memory(settings, history, provider)
        self.images = images
        self.agent = agent
        self.queue = TurnQueue(self)
        if self.agent:
            self.agent.memory = self.memory
            self.agent.tts = self.tts

    async def reply(self, event, text):
        for part, entities in formatted_chunks(text):
            try:
                await event.reply(
                    part, formatting_entities=entities, parse_mode=None, link_preview=False
                )
            except errors.BadRequestError as exc:
                formatting_error = isinstance(
                    exc, (errors.EntityBoundsInvalidError, errors.EntitiesTooLongError)
                )
                if (
                    not formatting_error
                    and "ENTIT" not in getattr(exc, "message", "")
                    and "PARSE" not in getattr(exc, "message", "")
                ):
                    raise
                for fallback in chunks(plain_fallback(part, entities)):
                    await event.reply(fallback, parse_mode=None, link_preview=False)

    async def admin(self, event):
        # Saved Messages only: neither prompts nor another chat can grant admin access.
        if not (
            event.is_private
            and event.out
            and event.sender_id == self.self_id
            and event.chat_id == self.self_id
            and event.message.date >= self.started
        ):
            return
        text = (event.raw_text or "").strip().lower()
        if text not in {"/admin", "/stats", "/stats all", "/memory", "/queue"}:
            return
        if self.history.seen(self.self_id, event.id):
            return
        self.history.mark_seen(self.self_id, event.id)
        from .metrics import memory_report

        if text == "/admin":
            answer = "Команды владельца в Избранном:\n/stats: статистика и расходы за сегодня\n/stats all: за всё время\n/memory: состояние памяти\n/queue: незавершённые ответы и отправки"
        elif text == "/queue":
            answer = self.queue.report()
        elif text == "/memory":
            answer = memory_report(self.history, self.settings)
        else:
            metrics = getattr(self.provider, "metrics", None)
            answer = (
                metrics.report(all_time=text.endswith(" all"))
                if metrics
                else "Учёт пока не включён."
            )
        await self.reply(event, answer)

    async def eligible(self, event, *, restored=False):
        if (
            not event.is_private
            or event.out
            or not event.sender_id
            or event.sender_id == self.self_id
        ):
            return False
        if event.sender_id in {777000, 333000, 42777}:
            return False
        if self.settings.allowed_users and event.sender_id not in self.settings.allowed_users:
            return False
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return False
        message = event.message
        if getattr(message, "via_bot_id", None) or getattr(message, "action", None):
            return False
        if (
            (event.raw_text or "")
            .lstrip()
            .lower()
            .startswith(("[ии-ассистент]", "[ai assistant]", "[ai-assistant]"))
        ):
            return False
        if not restored and message.date < self.started:
            return False
        return True

    async def download(self, message, directory):
        file = message.file
        if not file:
            raise MediaError("Это вложение не поддерживается. Отправьте фото, документ или видео.")
        if file.size and file.size > self.settings.max_file_bytes:
            raise MediaError("Максимальный размер вложения — 20 МБ.")
        suffix = ".jpg" if message.photo else Path(file.name or "").suffix.lower()
        suffix = suffix or (file.ext or "").lower()
        if suffix not in SUPPORTED_EXT:
            mime = getattr(file, "mime_type", "")
            suffix = {
                "audio/ogg": ".ogg",
                "audio/opus": ".opus",
                "audio/mpeg": ".mp3",
                "audio/mp4": ".m4a",
                "audio/x-wav": ".wav",
                "audio/wav": ".wav",
                "audio/flac": ".flac",
            }.get(mime, suffix)
        if suffix not in SUPPORTED_EXT:
            raise MediaError(
                "Формат не поддерживается. Используйте JPEG/PNG/WebP, PDF, DOCX, XLSX, "
                "текст UTF-8, аудио MP3/OGG/WAV/M4A/FLAC или видео MP4/MOV/MKV/WebM/AVI."
            )
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("attachment" + suffix)

        async def transfer():
            size = 0
            with target.open("wb") as output:
                async for chunk in self.client.iter_download(
                    message.media, request_size=512 * 1024
                ):
                    size += len(chunk)
                    if size > self.settings.max_file_bytes:
                        raise MediaError("Максимальный размер вложения — 20 МБ.")
                    output.write(chunk)
            if size == 0:
                raise MediaError("Получен пустой файл.")

        try:
            await asyncio.wait_for(transfer(), timeout=90)
        except TimeoutError:
            raise MediaError("Не удалось загрузить файл за 90 секунд. Повторите отправку позже.")
        return target

    async def handle(self, event, messages=None):
        try:
            if not await self.eligible(event):
                return
            await self.queue.submit(event, messages or [event.message])
        except errors.FloodWaitError as exc:
            self.blocked_until = time.monotonic() + exc.seconds
            LOG.warning(
                "Telegram запросил паузу %s с; новые автоответы приостановлены.", exc.seconds
            )
        except errors.RPCError as exc:
            LOG.warning("Telegram отклонил операцию (%s).", type(exc).__name__)
        except Exception as exc:
            LOG.error("Обработка сообщения не завершена (%s).", type(exc).__name__)

    async def process(self, event, messages):
        user_id = event.sender_id
        turn = CURRENT_TURN.get()
        if time.monotonic() < self.blocked_until:
            raise RetryTurn(self.blocked_until - time.monotonic() + 1)
        request_epoch = self.history.epoch(user_id)
        if any(self.history.changed(user_id, message.id) for message in messages):
            return
        if not turn and self.history.seen(user_id, event.id):
            return
        for message in messages:
            if not turn:
                self.history.mark_seen(user_id, message.id)
            self.history.archive_incoming(user_id, message)
        if hasattr(self.client, "send_read_acknowledge"):
            try:
                await self.client.send_read_acknowledge(user_id, max_id=max(m.id for m in messages))
            except errors.RPCError as exc:
                LOG.warning("Отметка прочтения не отправлена (%s).", type(exc).__name__)
        text = event.raw_text or ""
        command = control_intent(text)
        tasks = self.agent.tasks if self.agent else None
        if command == "/ai_stop":
            if not self.history.is_paused(user_id):
                self.history.pause(user_id)
                if tasks:
                    await tasks.suspend(user_id)
                await self.reply(
                    event,
                    "Хорошо, больше не буду отвечать. Когда захочешь вернуться, напиши «Давай продолжим».",
                )
            return
        if command == "/ai_reset":
            if tasks:
                await tasks.suspend(user_id, forget=True)
            self.history.reset(user_id)
            if self.history.reserve_control(user_id):
                await self.reply(
                    event,
                    "Я очистил память ИИ о нашей переписке. Сообщения в Telegram и личном архиве владельца остались.",
                )
            return
        if command == "/my_memory":
            if self.history.reserve_control(user_id):
                await self.reply(event, self.history.knowledge.display(user_id))
            return
        if command == "/memory_retry":
            if self.history.reserve_control(user_id):
                with self.history.db:
                    count = self.history.db.execute(
                        "UPDATE memory_sources SET state='pending',attempts=0,retry_at=0 WHERE user_id=? AND state='failed'",
                        (user_id,),
                    ).rowcount
                self.memory.wakeup.set()
                await self.reply(
                    event,
                    f"Вернул на разбор сообщений: {count}."
                    if count
                    else "Неудачных разборов нет. Новые сведения сохраняются автоматически.",
                )
            return
        confirmed = re.fullmatch(r"(?:/confirm|подтверди факт)\s+#?(\d+)[.!?]?", text.strip(), re.I)
        if confirmed:
            if self.history.reserve_control(user_id):
                try:
                    self.history.knowledge.confirm(user_id, int(confirmed[1]), event.id)
                    await self.reply(event, "Подтверждение сохранено. Буду учитывать это сведение.")
                except ValueError as exc:
                    await self.reply(event, str(exc))
            return
        forgotten = re.fullmatch(
            r"(?:/forget|забудь факт|удали факт)\s+#?(\d+)[.!?]?", text.strip(), re.I
        )
        forgotten_field = re.fullmatch(
            r"(?:забудь|удали из памяти)\s+(?:мо[йюеё]\s+)?(возраст|имя|фамилию|дату рождения|город)[.!?]?",
            text.strip(),
            re.I,
        )
        if forgotten or forgotten_field:
            if not self.history.reserve_control(user_id):
                return
            try:
                if forgotten:
                    self.history.knowledge.forget(user_id, int(forgotten[1]), self.history)
                else:
                    field = {
                        "возраст": "age",
                        "имя": "first_name",
                        "фамилию": "last_name",
                        "дату рождения": "birth_date",
                        "город": "city",
                    }[forgotten_field[1].lower()]
                    self.history.knowledge.forget_slot(user_id, "profile." + field, self.history)
                if tasks:
                    await tasks.invalidate_context(user_id)
                await self.reply(
                    event,
                    "Убрал факт и связанные фрагменты рабочей памяти. Старые разговоры больше не использую для восстановления забытого; остальные отдельные сведения продолжаю учитывать. Личный архив владельца остался.",
                )
            except ValueError as exc:
                await self.reply(event, str(exc))
            return
        if command == "/ai_start":
            self.history.pause(user_id, False)
            if tasks:
                tasks.wakeup.set()
        elif self.history.is_paused(user_id):
            return
        task_command = task_control(text)
        if task_command and tasks:
            if not self.history.reserve_control(user_id):
                return
            action, task_id = task_command
            try:
                if action == "cancel":
                    if task_id is None:
                        active = [
                            r
                            for r in tasks.store.list(user_id)
                            if r["status"] in {"В очереди", "Выполняется", "Нужно уточнение"}
                        ]
                        if len(active) != 1:
                            await self.reply(
                                event, "Укажи номер: «Отмени задачу 3».\n\n" + tasks.report(user_id)
                            )
                            return
                        task_id = active[0]["id"]
                    result = await tasks.cancel(user_id, task_id)
                    await self.reply(
                        event,
                        f"Задача #{task_id}: {result['status']}. Уже отправленные результаты остаются в чате.",
                    )
                else:
                    await self.reply(event, tasks.report(user_id, task_id))
            except ValueError as exc:
                await self.reply(event, str(exc))
            return
        is_control = command in {
            "/ai_start",
            "/ai_help",
            "/ai_voice_on",
            "/ai_voice_off",
        } or command.startswith("/ai_recall")
        permitted = bool(turn and turn.data.get("reserved")) or (
            self.history.reserve_control(user_id)
            if is_control
            else self.history.reserve(user_id, min_interval=0 if turn else 10)
        )
        if not permitted:
            if turn:
                raise RetryTurn(60 if is_control else self.history.reserve_delay(user_id))
            return
        if turn:
            turn.data["reserved"] = True
            turn.save()
        if command in {"/ai_start", "/ai_help"}:
            await self.reply(
                event, HELP if command == "/ai_help" else "Я снова на связи. О чём поговорим?"
            )
            return
        if command.startswith("/ai_recall"):
            query = text.partition(" ")[2].strip()
            if not query:
                await self.reply(event, "Укажите слова: /ai_recall отпуск бюджет")
            else:
                found = self.history.retrieve(user_id, query)
                result = "\n\n".join(
                    f"Архив #{row_id}:\n" + self.memory.relevant_fragment(raw, query, 2500)
                    for row_id, raw in found
                )
                await self.reply(event, result or "Совпадений в вашем сохранённом архиве нет.")
            return
        if command in {"/ai_voice_on", "/ai_voice_off"}:
            enabled = command == "/ai_voice_on"
            self.history.set_voice(user_id, enabled)
            notice = (
                "Хорошо, буду отвечать голосом и оставлять текст, чтобы было удобно перечитать."
                if enabled
                else "Хорошо, буду писать текстом, без голосовых."
            )
            if enabled and (not self.settings.fish_key or self.settings.voice_replies == "off"):
                notice = "Голос пока недоступен, поэтому буду отвечать текстом. Владелец аккаунта сможет его подключить."
            await self.reply(event, notice)
            return
        activity = (
            self.client.action(user_id, "typing")
            if hasattr(self.client, "action")
            else nullcontext()
        )
        for message in messages:
            self.memory.enqueue(user_id, message)
        async with self.slots, activity:
            try:
                prompt = image_prompt(text)
                if prompt is not None and self.images:
                    if any(
                        message.media and not getattr(message, "web_preview", None)
                        for message in messages
                    ):
                        raise MediaError(
                            "Сейчас поддерживается генерация по тексту. Редактирование приложенного изображения ещё не подключено."
                        )
                    target = (
                        self.settings.data_dir
                        / "attachments"
                        / str(user_id)
                        / f"generated-{event.id}.png"
                    )
                    if turn:
                        turn.sending("image")
                    generated = (
                        target if target.is_file() else await self.images.generate(prompt, target)
                    )
                    if self.history.epoch(user_id) != request_epoch:
                        return
                    content = [
                        {
                            "type": "text",
                            "text": "Запрос на создание картинки: "
                            + prompt
                            + "\nСгенерированный результат:",
                        },
                        {"type": "image_url", "image_url": {"url": image_data(generated)}},
                    ]
                    self.history.add(
                        user_id, event.id, content, "Создано изображение " + IMAGE_MODEL
                    )
                    await self.client.send_file(
                        user_id,
                        str(generated),
                        force_document=False,
                        caption="Картинка готова ✨",
                        parse_mode=None,
                        reply_to=event.id,
                    )
                    return
                combined = Prepared()
                captions = []
                with tempfile.TemporaryDirectory(prefix="tgaibot-") as temp:
                    for index, message in enumerate(messages):
                        if message.message:
                            captions.append(message.message)
                        geo = getattr(message, "geo", None)
                        if geo:
                            captions.append(
                                f"Пользователь прислал точку: широта {geo.lat}, долгота {geo.long}."
                            )
                            continue
                        if message.media and not getattr(message, "web_preview", None):
                            path = await self.download(message, Path(temp) / str(index))
                            self.history.archive_attachment(user_id, message.id, path)
                            part = await prepare_isolated(path, self.settings)
                            if (
                                getattr(message, "voice", False)
                                or getattr(message, "video_note", False)
                            ) and "Расшифровка речи (может содержать ошибки):" in part.text:
                                try:
                                    await self.client(
                                        functions.messages.ReadMessageContentsRequest(
                                            id=[message.id]
                                        )
                                    )
                                except errors.RPCError as exc:
                                    LOG.warning(
                                        "Отметка прослушивания не отправлена (%s).",
                                        type(exc).__name__,
                                    )
                            if (
                                getattr(message, "video", False)
                                or getattr(message, "video_note", False)
                            ) and getattr(self, "perception", None):
                                part = await self.perception.video(part, message.message or "")
                            if self.history.epoch(user_id) == request_epoch:
                                self.memory.enqueue_voice(user_id, message, part)
                            combined.text += "\n" + part.text
                            combined.images.extend(part.images)
                            combined.notes.extend(part.notes)
                    caption = "\n".join(captions)
                    if not caption and any(
                        getattr(m, "voice", False) or getattr(m, "audio", False) for m in messages
                    ):
                        caption = "Это моё аудиосообщение. Ответь на просьбу в расшифровке как на обычное сообщение; если текст неясен, уточни."
                    content = combined.content(caption)
                    if text_size(content) > 45_000 or image_count(content) > 16:
                        raise MediaError(
                            "Вложений слишком много: отправьте их по одному "
                            "(до 16 изображений и 45000 символов на запрос)."
                        )
                    epoch = request_epoch
                    if self.history.epoch(user_id) != epoch:
                        return
                    if turn and "input" in turn.data:
                        saved = turn.data["input"]
                        content, history, memory_notice = (
                            saved["content"],
                            saved["history"],
                            saved["notice"],
                        )
                    else:
                        history, memory_notice = await self.memory.context(user_id, content)
                        if turn:
                            turn.data["input"] = {
                                "content": content,
                                "history": history,
                                "notice": memory_notice,
                            }
                            turn.save()
                    if self.history.epoch(user_id) != epoch:
                        return
                    model_messages = [*history, {"role": "user", "content": content}]
                    background = background_request(text)
                    if turn and "answer" in turn.data:
                        from .agent import AgentReply

                        answer = AgentReply(
                            turn.data["answer"],
                            voice_attempted=turn.data.get("voice_attempted", False),
                        )
                    elif background and tasks:
                        if turn:
                            turn.seal()
                        created = tasks.create(
                            user_id,
                            event.id,
                            model_messages,
                            title=background[:110],
                            instruction=background,
                            delay_minutes=0,
                        )
                        answer = f"Принял задачу #{created['task_id']}. Займусь ею в фоне и пришлю результат сюда. Можно продолжать переписку или спросить «Что сделано?»."
                    elif self.agent:
                        answer = await self.agent.answer(
                            model_messages,
                            user_id,
                            event.id,
                            lambda: self.history.epoch(user_id) == epoch,
                            voice_default=self.settings.voice_replies == "always"
                            or any(getattr(m, "voice", False) for m in messages),
                        )
                    else:
                        answer = await self.provider.answer(model_messages)
                    if self.history.epoch(user_id) != epoch:
                        return
                    if turn:
                        turn.data["answer"] = str(answer)
                        turn.data["voice_attempted"] = getattr(answer, "voice_attempted", False)
                        turn.sending("final")
                    self.history.add(
                        user_id, event.id, content, answer, source_ids=[m.id for m in messages]
                    )
                    notice = "\n\n".join(dict.fromkeys(combined.notes))
                    if memory_notice:
                        notice += "\n\n" + memory_notice
                    await self.reply(
                        event, (notice.strip() + "\n\n" if notice.strip() else "") + answer
                    )
                    wants_voice = self.history.voice_enabled(
                        user_id,
                        self.settings.voice_replies == "always"
                        or any(getattr(m, "voice", False) for m in messages),
                    )
                    if (
                        self.tts
                        and wants_voice
                        and self.settings.voice_replies != "off"
                        and not getattr(answer, "voice_attempted", False)
                    ):
                        try:
                            if turn:
                                turn.sending("auto_voice")
                            voice, truncated = await self.tts.voice_note(answer, Path(temp))
                            if self.history.epoch(user_id) != epoch:
                                return
                            caption = "Голосовой ответ"
                            if truncated:
                                caption += "; прочитано начало, полный ответ выше."
                            await self.client.send_file(
                                user_id,
                                str(voice),
                                voice_note=True,
                                caption=caption,
                                parse_mode=None,
                                reply_to=event.id,
                            )
                        except (TTSError, MediaError) as exc:
                            LOG.warning("Озвучка не завершена: %s", exc)
                            await self.reply(event, simple_error(exc))
            except (MediaError, ProviderError, ValueError) as exc:
                LOG.warning("Запрос не завершён: %s", exc)
                await self.reply(
                    event, str(exc) if isinstance(exc, ValueError) else simple_error(exc)
                )


async def run(settings):
    from .metrics import Metrics

    metrics = Metrics(settings.data_dir)
    client = make_client(settings)
    provider, history, tts, images = (
        Provider(settings, metrics=metrics),
        History(settings),
        FishTTS(settings, metrics=metrics),
        ImageGenerator(settings, metrics=metrics),
    )
    maintenance = None
    calls = None
    search = Search(settings)
    archive = Archive(settings)
    archiver = Archiver(archive, client)
    web_server = None
    web_task = None
    task_manager = None
    memory = None
    perception = None
    archive.call_journal = history.calls
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Сначала выполните python -m tgaibot login в интерактивном терминале."
            )
        me = await client.get_me()
        if me.bot:
            raise RuntimeError("Нужна сессия обычного Telegram-аккаунта.")
        client.archive_sink = archiver.observe

        @client.on(events.Raw())
        async def call_event(update):
            from telethon.tl.types import PhoneCallDiscarded, PhoneCallRequested, UpdatePhoneCall

            if not isinstance(update, UpdatePhoneCall):
                return
            call = update.phone_call
            if isinstance(call, PhoneCallRequested):
                identifier = history.calls.incoming(call.admin_id, call.id)
                if not settings.enable_calls or not calls or not calls.enabled:
                    history.calls.update(
                        identifier,
                        "missed",
                        "disabled" if not settings.enable_calls else "unavailable",
                    )
            elif isinstance(call, PhoneCallDiscarded):
                row = history.db.execute(
                    "SELECT id,status FROM call_events WHERE telegram_id=?", (call.id,)
                ).fetchone()
                if row and row[1] in {"ringing", "connecting"}:
                    history.calls.update(row[0], "missed", "unknown")

        @client.on(events.NewMessage())
        async def archive_new(event):
            await archiver.observe(event)

        @client.on(events.MessageEdited())
        async def archive_edit(event):
            await archiver.observe(event)
            if event.is_private and not event.out:
                affected = history.revise(
                    [event.id],
                    event.chat_id,
                    replacement=event.raw_text
                    or "[Вложение изменено; прежнее описание не актуально]",
                )
                if task_manager:
                    for uid in affected:
                        await task_manager.invalidate_context(uid)

        @client.on(events.MessageDeleted())
        async def archive_delete(event):
            archive.deleted(event.deleted_ids, event.chat_id)
            affected = history.revise(event.deleted_ids, event.chat_id)
            if task_manager:
                for uid in affected:
                    await task_manager.invalidate_context(uid)

        archiver.start()
        if settings.archive_password:
            import os

            import uvicorn

            from .archive_web import create_app

            web_server = uvicorn.Server(
                uvicorn.Config(
                    create_app(settings, archiver, metrics=metrics, history=history),
                    host="0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT_ID") else "127.0.0.1",
                    port=settings.archive_port,
                    log_level="warning",
                    access_log=False,
                )
            )
            web_task = asyncio.create_task(web_server.serve())
        bot = Userbot(
            settings,
            client,
            provider,
            history,
            me.id,
            tts,
            images,
            Agent(settings, provider, search, client),
        )
        from .tasks import TaskManager

        task_manager = TaskManager(settings, bot.agent, history)
        bot.agent.tasks = task_manager
        task_manager.start()
        memory = bot.memory
        memory.start()
        from .call_brain import Perception

        perception = Perception(settings, metrics)
        bot.perception = perception
        if settings.enable_calls:
            from .calls import CallBridge, call_readiness

            missing = call_readiness(settings)
            if missing:
                LOG.warning(
                    "Звонки отключены; сообщения работают. Не хватает: %s", ", ".join(missing)
                )
            else:
                calls = CallBridge(bot)
                try:
                    await calls.start()
                    bot.agent.call_bridge = calls
                except Exception as exc:
                    LOG.warning("Звонки не запущены (%s); сообщения работают.", type(exc).__name__)
                    await calls.close()
                    calls = None

        @client.on(events.NewMessage(incoming=True))
        async def incoming(event):
            if not event.grouped_id:
                await bot.handle(event)

        @client.on(events.NewMessage(outgoing=True))
        async def admin_message(event):
            await bot.admin(event)

        @client.on(events.Album())
        async def album(event):
            # Adapt Album to NewMessage so access checks, dedup and commands stay identical.
            first = event.messages[0]
            adapted = events.NewMessage.Event(first)
            adapted._set_client(client)
            await bot.handle(adapted, event.messages)

        async def cleanup():
            while True:
                await asyncio.sleep(3600)
                history.purge()

        maintenance = asyncio.create_task(cleanup())
        bot.queue.recovery = asyncio.create_task(bot.queue.restore())
        bot.queue.start()
        print(
            "Юзербот запущен. Очередь ответов и восстановление после перезапуска включены. Ctrl+C — остановить."
        )
        if web_task:
            telegram_task = asyncio.create_task(client.run_until_disconnected())
            await asyncio.wait([telegram_task, web_task], return_when=asyncio.FIRST_COMPLETED)
            if web_task.done():
                await client.disconnect()
            await telegram_task
            if web_task.done() and web_task.exception():
                raise RuntimeError(
                    "Не удалось запустить просмотр архива. Проверьте свободный порт."
                )
        else:
            await client.run_until_disconnected()
    finally:
        if "bot" in locals():
            await bot.queue.close()
        if web_server:
            web_server.should_exit = True
        if web_task:
            try:
                await asyncio.wait_for(asyncio.gather(web_task, return_exceptions=True), 10)
            except TimeoutError:
                web_task.cancel()
        if maintenance:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
        if calls:
            await calls.close()
        if task_manager:
            await task_manager.close()
        if memory:
            await memory.close()
        if perception:
            await perception.close()
        await client.disconnect()
        await archiver.close()
        archive.close()
        await provider.close()
        await tts.close()
        await images.close()
        await search.close()
        history.close()
        metrics.close()
