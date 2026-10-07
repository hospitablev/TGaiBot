"""Inbound-only calls with barge-in, bounded latency and durable recordings."""

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

from .call_audio import BPS, MAX_SECONDS, RATE, REACTIONS, CallRecorder, ReactionBank, to_pcm
from .call_brain import CallBrain
from .tts import display_speech, speech_text

LOG = logging.getLogger(__name__)
SAMPLE_RATE = RATE
BYTES_PER_SECOND = BPS


def call_readiness(settings):
    missing = []
    for package in ("pytgcalls", "ntgcalls"):
        if importlib.util.find_spec(package) is None:
            missing.append(package)
    if not settings.fish_key:
        missing.append("FISH_API_KEY")
    if not settings.transcription:
        missing.append("ENABLE_TRANSCRIPTION=true")
    if not settings.call_users and not settings.call_usernames:
        missing.append("CALL_ALLOWED_USER_IDS")
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            missing.append(executable)
    return missing


class SpeechBuffer:
    """PCM16 VAD with noise floor, pre-roll and 600 ms end-of-turn pause."""

    def __init__(self):
        self.data = bytearray()
        self.pre = bytearray()
        self.silence = 0
        self.voiced = 0
        self.noise = 100.0

    def feed(self, frame):
        if len(frame) < 2 or len(frame) % 2:
            return None
        samples = array("h", frame)
        energy = (sum(v * v for v in samples) / len(samples)) ** 0.5
        loud = energy > max(350, self.noise * 2.8)
        if not loud and not self.data:
            self.noise = 0.98 * self.noise + 0.02 * min(energy, 250)
        if loud:
            if not self.data:
                self.data.extend(self.pre)
                self.pre.clear()
            self.voiced += len(frame)
            self.silence = 0
        elif self.data:
            self.silence += len(frame)
        if loud or self.data:
            self.data.extend(frame)
        else:
            self.pre.extend(frame)
            self.pre = self.pre[-int(0.12 * BPS) :]
        if self.silence >= int(0.6 * BPS) or len(self.data) >= 20 * BPS:
            result = bytes(self.data) if self.voiced >= int(0.25 * BPS) else None
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
        self.turn_task = None
        self.chat_id = None
        self.listening = False
        self.buffer = SpeechBuffer()
        self.queue = asyncio.Queue(maxsize=2)
        self.ended = asyncio.Event()
        self.enabled = False
        self.generation = 0
        self.identifier = None
        self.allowed = set(self.settings.call_users)
        self.recorder = None
        self.bank = ReactionBank(self.settings, bot.tts)
        self.brain = CallBrain(bot)
        self.warm_task = None
        self.play_lock = asyncio.Lock()
        self.last_filler = 0
        self.journal = bot.history.calls
        self.failure_reason = None
        self.start_time = 0

    async def start(self):
        from pytgcalls import PyTgCalls, filters
        from pytgcalls.types import ChatUpdate, Device, Direction

        for username in self.settings.call_usernames:
            try:
                entity = await self.bot.client.get_entity(username)
                if not getattr(entity, "bot", False):
                    self.allowed.add(entity.id)
            except Exception:
                LOG.warning("Не удалось разрешить разрешённое имя звонящего.")
        self.app = PyTgCalls(self.bot.client)

        @self.app.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))
        async def incoming(_, update):
            await self.incoming(update.chat_id)

        @self.app.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def ended(_, update):
            if update.chat_id == self.chat_id:
                self.ended.set()
                if self.task:
                    self.task.cancel()

        @self.app.on_update(filters.stream_frame(directions=Direction.INCOMING))
        async def frames(_, update):
            if update.chat_id != self.chat_id or update.device not in (
                Device.MICROPHONE,
                Device.SPEAKER,
            ):
                return
            for frame in update.frames:
                self.feed(frame.frame)

        await self.app.start()
        self.enabled = True
        self.warm_task = asyncio.create_task(self.prepare())
        LOG.warning("Входящие звонки готовы; разрешённых собеседников: %s.", len(self.allowed))

    async def prepare(self):
        rows = self.journal.db.execute(
            "SELECT id FROM call_events WHERE recording IS NULL AND status IN ('failed','ended') ORDER BY id DESC LIMIT 50"
        ).fetchall()
        for (identifier,) in rows:
            root = self.settings.data_dir / "call-recordings" / str(identifier)
            if not root.is_dir():
                continue
            try:
                path = root / "recording.ogg"
                if not path.is_file():
                    path = await CallRecorder.encode(self.settings, root)
                if path:
                    self.journal.recording(identifier, path, 0)
            except Exception:
                LOG.warning("Не удалось восстановить запись звонка %s.", identifier)
        await self.bank.prepare()

    async def incoming(self, chat_id):
        identifier = self.journal.incoming(chat_id)
        reason = None
        if not self.enabled:
            reason = "unavailable"
        elif chat_id not in self.allowed or chat_id == self.bot.self_id:
            reason = "not_allowed"
        elif self.chat_id is not None:
            reason = "busy"
        elif self.bot.history.is_paused(chat_id):
            reason = "paused"
        elif time.monotonic() < self.bot.blocked_until:
            reason = "limited"
        elif not self.bot.history.reserve_control(chat_id):
            reason = "limited"
        if reason:
            self.journal.update(identifier, "declined", reason)
            return
        self.chat_id = chat_id
        self.identifier = identifier
        self.failure_reason = None
        self.ended.clear()
        self.buffer = SpeechBuffer()
        self.generation += 1
        self.journal.update(identifier, "connecting")
        self.task = asyncio.create_task(self.session(chat_id))

    def feed(self, frame):
        if not self.listening or self.ended.is_set():
            return
        if self.recorder:
            self.recorder.write("user", frame)
        segment = self.buffer.feed(frame)
        if (
            (self.buffer.voiced >= int(0.18 * BPS) or segment)
            and self.turn_task
            and not self.turn_task.done()
            and not self.turn_task.cancelling()
        ):
            self.generation += 1
            self.turn_task.cancel()
            if self.identifier:
                self.journal.turn(self.identifier, interrupted=True)
        if segment:
            if self.queue.full():
                self.queue.get_nowait()
            self.queue.put_nowait(segment)

    async def play_pcm(self, chat_id, path):
        from pytgcalls.types import Device

        generation = self.generation
        async with self.play_lock:
            start = time.monotonic()
            index = 0
            with Path(path).open("rb") as audio:
                while data := audio.read(1920):
                    if (
                        self.ended.is_set()
                        or generation != self.generation
                        or self.bot.history.is_paused(chat_id)
                    ):
                        return
                    frame = data.ljust(1920, b"\0")
                    await self.app.send_frame(chat_id, Device.MICROPHONE, frame)
                    if self.recorder:
                        self.recorder.write("assistant", frame)
                    index += 1
                    await asyncio.sleep(max(0, start + index * 0.02 - time.monotonic()))

    async def speak(self, chat_id, text, directory):
        spoken, _ = speech_text(text, limit=1000)
        mp3 = await self.bot.tts.synthesize(spoken, directory / "speech.mp3")
        pcm = await to_pcm(self.settings, mp3, directory / "speech.pcm")
        await self.play_pcm(chat_id, pcm)

    async def clip(self, chat_id, key, directory):
        path = self.bank.path(key)
        if path:
            await self.play_pcm(chat_id, path)
        else:
            await self.speak(chat_id, REACTIONS[key], directory)

    async def thinking(self, chat_id):
        await asyncio.sleep(1.4)
        if self.buffer.voiced or time.monotonic() - self.last_filler < 12:
            return
        key = self.bank.choose("think")
        if key:
            self.last_filler = time.monotonic()
            await self.play_pcm(chat_id, self.bank.path(key))

    async def process(self, chat_id, segment, directory):
        generation = self.generation
        epoch = self.bot.history.epoch(chat_id)

        def valid():
            return (
                not self.ended.is_set()
                and generation == self.generation
                and self.bot.history.epoch(chat_id) == epoch
            )

        identifier = -time.time_ns()
        answer = ""
        start = time.monotonic()
        filler = asyncio.create_task(self.thinking(chat_id))
        try:
            path = directory / "utterance.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(RATE)
                audio.writeframes(segment)
            async with asyncio.timeout(40):
                plan = await self.brain.understand(chat_id, path)
                if not valid():
                    return
                transcript = plan["transcript"].strip()
                if not transcript:
                    await self.clip(chat_id, "repeat_1", directory)
                    return
                self.journal.transcript(self.identifier, "user", transcript)
                filler.cancel()
                await asyncio.gather(filler, return_exceptions=True)
                group = (
                    "search"
                    if plan["action"] == "search"
                    else "listen"
                    if plan["action"] == "reply" and plan["reaction"] == "listen"
                    else None
                )
                if group and self.last_filler < start:
                    key = self.bank.choose(group)
                    if key:
                        self.last_filler = time.monotonic()
                        await self.play_pcm(chat_id, self.bank.path(key))
                answer = await self.brain.act(chat_id, identifier, plan, valid)
                if not valid():
                    return
                if plan["action"] == "task" and self.last_filler < start and "сохранил" in answer:
                    key = self.bank.choose("task")
                    if key:
                        self.last_filler = time.monotonic()
                        await self.play_pcm(chat_id, self.bank.path(key))
                filler.cancel()
                await asyncio.gather(filler, return_exceptions=True)
                self.journal.turn(self.identifier, latency_ms=(time.monotonic() - start) * 1000)
                await self.speak(chat_id, answer, directory)
                if not valid():
                    return
                self.journal.transcript(self.identifier, "assistant", display_speech(answer))
                self.bot.history.add(chat_id, identifier, transcript, display_speech(answer))
                self.bot.history.knowledge.enqueue(chat_id, identifier, transcript, origin="voice")
                self.bot.memory.wakeup.set()
        except asyncio.CancelledError:
            if answer:
                self.journal.transcript(
                    self.identifier, "assistant", display_speech(answer), interrupted=True
                )
            raise
        except Exception as exc:
            LOG.warning("Реплика звонка не завершена (%s).", type(exc).__name__)
            if valid():
                with suppress(Exception):
                    await asyncio.wait_for(self.clip(chat_id, "connection", directory), 12)
        finally:
            filler.cancel()
            await asyncio.gather(filler, return_exceptions=True)

    async def session(self, chat_id):
        from ntgcalls import MediaSource
        from pytgcalls.types import CallConfig, RecordStream
        from pytgcalls.types.raw import AudioParameters, AudioStream, Stream

        if self.identifier is None:
            self.identifier = self.journal.incoming(chat_id)
        connected = False
        self.start_time = time.monotonic()
        try:
            async with asyncio.timeout(MAX_SECONDS):
                with tempfile.TemporaryDirectory(prefix="tgaibot-call-") as temp:
                    directory = Path(temp)
                    stream = Stream(
                        microphone=AudioStream(MediaSource.EXTERNAL, "", AudioParameters(RATE, 1))
                    )
                    await self.app.play(chat_id, stream, CallConfig(timeout=30))
                    connected = True
                    self.recorder = CallRecorder(self.settings, self.identifier)
                    await self.app.record(
                        chat_id, RecordStream(audio=True, audio_parameters=AudioParameters(RATE, 1))
                    )
                    self.journal.update(self.identifier, "active")
                    self.listening = True
                    self.turn_task = asyncio.create_task(self.clip(chat_id, "hello", directory))
                    try:
                        await self.turn_task
                    except asyncio.CancelledError:
                        if self.ended.is_set():
                            raise
                    idle = 0
                    utterances = 0
                    while not self.ended.is_set() and not self.bot.history.is_paused(chat_id):
                        try:
                            segment = await asyncio.wait_for(self.queue.get(), 45)
                        except TimeoutError:
                            if idle:
                                break
                            idle += 1
                            await self.clip(chat_id, "silence", directory)
                            continue
                        idle = 0
                        utterances += 1
                        if utterances > 120:
                            self.failure_reason = "limited"
                            break
                        self.turn_task = asyncio.create_task(
                            self.process(chat_id, segment, directory)
                        )
                        try:
                            await self.turn_task
                        except asyncio.CancelledError:
                            if self.ended.is_set():
                                raise
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            self.failure_reason = "timeout"
        except Exception as exc:
            self.failure_reason = "processing" if connected else "connection"
            LOG.warning("Звонок завершён (%s).", type(exc).__name__)
        finally:
            self.listening = False
            self.ended.set()
            if self.turn_task and not self.turn_task.done():
                self.turn_task.cancel()
                await asyncio.gather(self.turn_task, return_exceptions=True)
            with suppress(Exception):
                await asyncio.wait_for(self.app.leave_call(chat_id), 5)
            self.journal.update(
                self.identifier,
                "failed" if self.failure_reason else "ended",
                self.failure_reason or "ended",
            )
            if self.recorder:
                try:
                    path = await self.recorder.finish()
                    self.journal.recording(
                        self.identifier, path or "", time.monotonic() - self.start_time
                    )
                except Exception:
                    LOG.warning("Запись звонка оставлена в PCM для восстановления.")
            self.recorder = None
            self.chat_id = None
            self.turn_task = None
            while not self.queue.empty():
                self.queue.get_nowait()

    async def close(self):
        self.enabled = False
        self.ended.set()
        if self.task and not self.task.done():
            self.failure_reason = "restart"
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.warm_task:
            self.warm_task.cancel()
            await asyncio.gather(self.warm_task, return_exceptions=True)
        await self.brain.close()
