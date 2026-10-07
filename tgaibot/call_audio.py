"""Bounded PCM recording and reusable short reactions for live calls."""

import asyncio
import hashlib
import random
import time
from pathlib import Path

from .media import run_tool

RATE = 48000
BPS = RATE * 2
MAX_SECONDS = 600
REACTIONS = {
    "listen_1": "Угу, слушаю.",
    "listen_2": "Ага, продолжай.",
    "listen_3": "Да, я здесь.",
    "listen_4": "Мм, слушаю тебя.",
    "listen_5": "Угу.",
    "listen_6": "Ага.",
    "listen_7": "Я на связи.",
    "listen_8": "Рассказывай.",
    "think_1": "[short pause] Секунду.",
    "think_2": "Мм, сейчас.",
    "think_3": "Дай подумать.",
    "think_4": "Сейчас разберусь.",
    "think_5": "Одну секунду.",
    "think_6": "Хм, посмотрим.",
    "repeat_1": "Не расслышал конец. Повторишь?",
    "repeat_2": "Можешь сказать ещё раз?",
    "repeat_3": "Связь прервалась, повтори последнюю фразу.",
    "repeat_4": "Что ты сказала?",
    "task_1": "Принял, займусь этим.",
    "task_2": "Хорошо, задачу сохранил.",
    "task_3": "Результат пришлю в переписку.",
    "task_4": "Займусь этим, а мы можем продолжить.",
    "search_1": "Сейчас проверю.",
    "search_2": "Поищу свежую информацию.",
    "search_3": "Секунду, уточню.",
    "search_4": "Посмотрю, что удалось найти.",
    "warm_1": "[warm tone] Понимаю тебя.",
    "warm_2": "[soft tone] Я слушаю.",
    "warm_3": "[chuckle] Забавно.",
    "warm_4": "[warm tone] Здорово.",
    "hello": "Привет! Наш разговор сохраняется в архиве, как переписка. Я слушаю.",
    "bye": "Пока! Если что, пиши.",
    "connection": "Сейчас не получается обработать ответ. Давай попробуем ещё раз или продолжим в переписке.",
    "silence": "Ты ещё здесь?",
}


async def to_pcm(settings, source, target):
    await asyncio.to_thread(
        run_tool,
        [
            settings.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(RATE),
            "-f",
            "s16le",
            "-t",
            "90",
            str(target),
        ],
    )
    return target


class ReactionBank:
    def __init__(self, settings, tts):
        self.settings, self.tts = settings, tts
        fingerprint = hashlib.sha256(
            f"{settings.fish_model}|{settings.fish_voice}|{settings.fish_speed}".encode()
        ).hexdigest()[:16]
        self.root = settings.data_dir / "call-sounds" / fingerprint
        self.bundled = Path(__file__).parent / "assets" / "call-sounds" / fingerprint
        self.root.mkdir(parents=True, exist_ok=True)
        self.previous = {}

    def path(self, key):
        if key not in REACTIONS:
            return None
        digest = hashlib.sha256(REACTIONS[key].encode()).hexdigest()[:8]
        filename = f"{key}-{digest}.pcm"
        for root in (self.root, self.bundled):
            path = root / filename
            if path.is_file() and 0 < path.stat().st_size <= 20 * BPS:
                return path
        return None

    def choose(self, group):
        choices = [
            key
            for key in REACTIONS
            if key.startswith(group + "_") and self.path(key) and key != self.previous.get(group)
        ]
        if not choices:
            return None
        key = random.choice(choices)
        self.previous[group] = key
        return key

    async def prepare(self):
        # Sequential requests avoid flooding Fish and do not block call acceptance.
        for key, text in REACTIONS.items():
            if self.path(key):
                continue
            digest = hashlib.sha256(text.encode()).hexdigest()[:8]
            target = self.root / f"{key}-{digest}.pcm"
            mp3 = target.with_suffix(".mp3")
            part = target.with_suffix(".part")
            try:
                await self.tts.synthesize(text, mp3)
                await to_pcm(self.settings, mp3, part)
                if not 0 < part.stat().st_size <= 20 * BPS:
                    raise ValueError("Reaction too long")
                part.replace(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A later restart retries missing clips; never loop on a failed paid request.
                continue
            finally:
                mp3.unlink(missing_ok=True)
                part.unlink(missing_ok=True)


class CallRecorder:
    def __init__(self, settings, identifier):
        self.settings = settings
        self.root = settings.data_dir / "call-recordings" / str(identifier)
        self.root.mkdir(parents=True, exist_ok=True)
        self.start = time.monotonic()
        self.files = {
            role: (self.root / f"{role}.pcm").open("wb") for role in ("user", "assistant")
        }
        self.closed = False

    def write(self, role, data):
        if self.closed or len(data) % 2:
            return
        stream = self.files[role]
        position = max(stream.tell(), int((time.monotonic() - self.start) * RATE) * 2)
        if position + len(data) > MAX_SECONDS * BPS:
            return
        stream.seek(position)
        stream.write(data)

    async def finish(self):
        if self.closed:
            return None
        self.closed = True
        for stream in self.files.values():
            stream.close()
        return await self.encode(self.settings, self.root)

    @staticmethod
    async def encode(settings, root):
        sources = [
            p
            for p in (root / "user.pcm", root / "assistant.pcm")
            if p.is_file() and p.stat().st_size
        ]
        if not sources:
            return None
        args = [settings.ffmpeg, "-nostdin", "-v", "error", "-y"]
        for path in sources:
            args += ["-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", str(path)]
        if len(sources) == 2:
            args += ["-filter_complex", "amix=inputs=2:duration=longest:normalize=0"]
        target = root / "recording.ogg"
        part = root / "recording.part.ogg"
        args += ["-c:a", "libopus", "-b:a", "48k", str(part)]
        await asyncio.to_thread(run_tool, args)
        part.replace(target)
        for path in sources:
            path.unlink(missing_ok=True)
        return target
