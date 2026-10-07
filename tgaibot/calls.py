"""Experimental inbound P2P audio: PCM -> local Whisper -> Claude -> Fish -> PCM.

No real call has been validated without an authorized Telegram account. Dependencies
are pinned because frame interfaces are version-specific. Never access local devices.
"""

import asyncio
import importlib.util
import logging
import shutil
import tempfile
import time
import wave
from array import array
from contextlib import suppress
from pathlib import Path

from .media import run_tool
from .tts import speech_text
from .worker import prepare_isolated

LOG = logging.getLogger(__name__)
SAMPLE_RATE = 48000
BYTES_PER_SECOND = SAMPLE_RATE * 2


def call_readiness(settings):
    missing = []
    for package in ("pytgcalls", "ntgcalls", "faster_whisper"):
        if importlib.util.find_spec(package) is None:
            missing.append(package)
    if not settings.fish_key:
        missing.append("FISH_API_KEY")
    if not settings.transcription:
        missing.append("ENABLE_TRANSCRIPTION=true")
    if not settings.call_users:
        missing.append("CALL_ALLOWED_USER_IDS")
    # PyTgCalls itself expects these executable names on PATH.
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            missing.append(executable)
    return missing


class SpeechBuffer:
    """Simple energy-based turn detection, mono PCM16/48kHz, max 20 seconds."""

    def __init__(self):
        self.data = bytearray()
        self.silence = 0
        self.voiced = 0

    def feed(self, frame):
        if len(frame) < 2 or len(frame) % 2:
            return None
        samples = array("h", frame)
        energy = sum(v * v for v in samples) / len(samples)
        loud = energy > 350 * 350
        if loud:
            self.voiced += len(frame)
            self.silence = 0
        elif self.data:
            self.silence += len(frame)
        if loud or self.data:
            self.data.extend(frame)
        if self.silence >= BYTES_PER_SECOND or len(self.data) >= 20 * BYTES_PER_SECOND:
            result = bytes(self.data) if self.voiced >= int(0.3 * BYTES_PER_SECOND) else None
            self.data.clear()
            self.silence = self.voiced = 0
            return result
        return None


class CallBridge:
    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.app = None
        self.task = None
        self.chat_id = None
        self.listening = False
        self.buffer = SpeechBuffer()
        self.queue = asyncio.Queue(maxsize=1)
        self.ended = asyncio.Event()
        self.enabled = False

    async def start(self):
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Direction

        self.app = PyTgCalls(self.bot.client)

        @self.app.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def incoming(_, update):
            chat_id = update.chat_id
            if (
                not self.enabled
                or self.chat_id is not None
                or chat_id not in self.settings.call_users
                or chat_id == self.bot.self_id
                or self.bot.history.is_paused(chat_id)
                or time.monotonic() < self.bot.blocked_until
            ):
                return
            sender = await self.bot.client.get_entity(chat_id)
            if getattr(sender, "bot", False) or not self.bot.history.reserve(chat_id):
                return
            if self.chat_id is not None:
                return
            self.chat_id = chat_id
            self.ended.clear()
            self.task = asyncio.create_task(self.session(chat_id))

        @self.app.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def ended(_, update):
            if update.chat_id == self.chat_id and not self.ended.is_set():
                self.ended.set()
                if self.task:
                    self.task.cancel()

        @self.app.on_update(filters.stream_frame(directions=Direction.INCOMING))
        async def frames(_, update):
            from pytgcalls.types import Device

            if (
                update.chat_id != self.chat_id
                or not self.listening
                or self.bot.history.is_paused(update.chat_id)
                or update.device not in (Device.MICROPHONE, Device.SPEAKER)
            ):
                return
            for frame in update.frames:
                segment = self.buffer.feed(frame.frame)
                if segment and not self.queue.full():
                    self.queue.put_nowait(segment)
                    self.listening = False
                    break

        await self.app.start()
        self.enabled = True
        LOG.warning("Экспериментальные входящие звонки включены только для CALL_ALLOWED_USER_IDS.")

    async def speak(self, chat_id, text, directory):
        from pytgcalls.types import Device

        spoken, _ = speech_text(text, limit=1000)
        mp3 = await self.bot.tts.synthesize(spoken, directory / "speech.mp3")
        pcm = directory / "speech.pcm"
        await asyncio.to_thread(
            run_tool,
            [
                self.settings.ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(mp3),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "s16le",
                "-t",
                "90",
                str(pcm),
            ],
        )
        start = time.monotonic()
        with pcm.open("rb") as audio:
            index = 0
            while data := audio.read(1920):
                if self.ended.is_set() or self.bot.history.is_paused(chat_id):
                    return
                await self.app.send_frame(chat_id, Device.MICROPHONE, data.ljust(1920, b"\0"))
                index += 1
                await asyncio.sleep(max(0, start + index * 0.02 - time.monotonic()))

    async def session(self, chat_id):
        from ntgcalls import MediaSource
        from pytgcalls.types import CallConfig, RecordStream
        from pytgcalls.types.raw import AudioParameters, AudioStream, Stream

        try:
            async with asyncio.timeout(180):
                with tempfile.TemporaryDirectory(prefix="tgaibot-call-") as temp:
                    directory = Path(temp)
                    # Empty external PCM source: never open the machine's microphone/camera.
                    stream = Stream(
                        microphone=AudioStream(
                            MediaSource.EXTERNAL, "", AudioParameters(SAMPLE_RATE, 1)
                        )
                    )
                    await self.app.play(chat_id, stream, CallConfig(timeout=30))
                    await self.app.record(
                        chat_id,
                        RecordStream(audio=True, audio_parameters=AudioParameters(SAMPLE_RATE, 1)),
                    )
                    await self.speak(
                        chat_id,
                        "Здравствуйте. Это ИИ-ассистент. Речь распознаётся локально; "
                        "текст обрабатывается Клодом, ответы озвучивает Fish Audio. "
                        "Говорите по очереди, короткими фразами. Я слушаю.",
                        directory,
                    )
                    while not self.ended.is_set() and not self.bot.history.is_paused(chat_id):
                        self.buffer = SpeechBuffer()
                        self.listening = True
                        segment = await asyncio.wait_for(self.queue.get(), timeout=45)
                        self.listening = False
                        path = directory / "utterance.wav"
                        with wave.open(str(path), "wb") as audio:
                            audio.setnchannels(1)
                            audio.setsampwidth(2)
                            audio.setframerate(SAMPLE_RATE)
                            audio.writeframes(segment)
                        async with self.bot.slots:
                            prepared = await prepare_isolated(path, self.settings)
                            if "Речь не найдена." in prepared.text:
                                continue
                            if not self.bot.history.reserve(chat_id, min_interval=0):
                                break
                            content = (
                                "Реплика собеседника в голосовом звонке. Ответь кратко, "
                                "до 600 символов, без Markdown.\n" + prepared.text
                            )
                            epoch = self.bot.history.epoch(chat_id)
                            context, _ = await self.bot.memory.context(chat_id, content)
                            answer = await self.bot.provider.answer(
                                [*context, {"role": "user", "content": content}], max_tokens=512
                            )
                            if self.bot.history.epoch(chat_id) != epoch:
                                continue
                            self.bot.history.add(chat_id, -time.time_ns(), content, answer)
                        await self.speak(chat_id, answer, directory)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOG.warning("Звонок завершён (%s); переписка остаётся доступна.", type(exc).__name__)
        finally:
            self.listening = False
            self.ended.set()
            with suppress(Exception):
                await self.app.leave_call(chat_id)
            self.chat_id = None
            while not self.queue.empty():
                self.queue.get_nowait()

    async def close(self):
        self.enabled = False
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
