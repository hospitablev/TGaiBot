import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors
from telethon.tl.types import MessageEntityBold

from tgaibot.provider import ProviderError
from tgaibot.tts import TTSError
from tgaibot.userbot import PREFIX, Userbot, account_lock, chunks


def event(text="Привет", *, user=10, mid=1, **kwargs):
    message = SimpleNamespace(
        id=mid,
        message=text,
        media=None,
        via_bot_id=None,
        action=None,
        date=datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    values = dict(
        is_private=True,
        out=False,
        sender_id=user,
        raw_text=text,
        id=mid,
        message=message,
        get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)),
        reply=AsyncMock(),
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


@pytest.fixture
def bot(settings, history):
    return Userbot(
        settings,
        SimpleNamespace(),
        SimpleNamespace(answer=AsyncMock(return_value="Ответ")),
        history,
        self_id=99,
    )


async def test_inbound_reply_discloses_ai_and_deduplicates(bot):
    incoming = event()
    await bot.handle(incoming)
    await bot.handle(incoming)
    assert bot.provider.answer.await_count == 1
    assert incoming.reply.await_args.args[0].startswith(PREFIX)
    assert "внешними ИИ-сервисами" in incoming.reply.await_args.args[0]
    assert incoming.reply.await_args.kwargs["parse_mode"] is None
    assert len(bot.history.messages(10)) == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"out": True},
        {"is_private": False},
        {"user": 99},
        {"user": 777000},
        {"text": "[ИИ-ассистент] привет"},
        {"get_sender": AsyncMock(return_value=SimpleNamespace(bot=True))},
    ],
)
async def test_loop_and_scope_filters(bot, kwargs):
    incoming = event(**kwargs)
    await bot.handle(incoming)
    bot.provider.answer.assert_not_awaited()
    incoming.reply.assert_not_awaited()


async def test_pause_resume_reset(bot):
    await bot.handle(event("/ai_stop"))
    await bot.handle(event("hello", mid=2))
    bot.provider.answer.assert_not_awaited()
    await bot.handle(event("/ai_start", mid=3))
    assert not bot.history.is_paused(10)
    bot.history.add(10, 4, "Private", "Answer")
    await bot.handle(event("/ai_reset", mid=5))
    assert not bot.history.messages(10)


async def test_no_history_on_provider_failure(bot):
    bot.provider.answer.side_effect = ProviderError("Провайдер недоступен.")
    incoming = event()
    await bot.handle(incoming)
    assert not bot.history.messages(10)
    assert "ИИ-сервис не смог ответить" in incoming.reply.await_args.args[0]


async def test_concurrent_duplicate_is_only_answered_once(bot):
    first = event()
    await asyncio.gather(bot.handle(first), bot.handle(first))
    assert bot.provider.answer.await_count == 1


async def test_actual_size_enforced_while_download(bot, tmp_path):
    async def download(*args, **kwargs):
        yield b"x" * (bot.settings.max_file_bytes + 1)

    bot.client.iter_download = download
    message = SimpleNamespace(
        file=SimpleNamespace(size=1, name="x.txt", ext=".txt"), photo=None, media=object()
    )
    from tgaibot.media import MediaError

    with pytest.raises(MediaError, match="20 МБ"):
        await bot.download(message, tmp_path)


def test_utf16_chunk_limits():
    original = "🙂" * 5000 + "Я" * 5000
    parts = list(chunks(original))
    assert "".join(parts) == original
    assert all(len((PREFIX + part).encode("utf-16-le")) // 2 < 4096 for part in parts)


def test_single_instance_lock(tmp_path):
    with account_lock(tmp_path):
        with pytest.raises(RuntimeError, match="уже запущена"):
            with account_lock(tmp_path):
                pass


async def test_text_survives_tts_failure(bot):
    bot.tts = SimpleNamespace(voice_note=AsyncMock(side_effect=TTSError("Озвучка недоступна")))
    bot.history.set_voice(10, True)
    incoming = event()
    await bot.handle(incoming)
    assert incoming.reply.await_count == 2
    assert "Ответ" in incoming.reply.await_args_list[0].args[0]
    assert "оставил его текстом" in incoming.reply.await_args_list[1].args[0]
    assert bot.history.messages(10)[-1]["content"] == "Ответ"


async def test_image_sent_as_file_not_link(bot, tmp_path):
    from PIL import Image

    path = tmp_path / "generated.png"
    Image.new("RGB", (16, 16), "red").save(path)
    bot.images = SimpleNamespace(generate=AsyncMock(return_value=path))
    bot.client.send_file = AsyncMock()
    await bot.handle(event("Нарисуй красный квадрат"))
    bot.images.generate.assert_awaited_once()
    bot.provider.answer.assert_not_awaited()
    assert bot.client.send_file.await_args.args[1] == str(path)
    assert bot.client.send_file.await_args.kwargs["force_document"] is False


async def test_natural_voice_switch_does_not_drop_immediate_question(bot):
    await bot.handle(event("Отвечай голосом", mid=1))
    assert bot.history.voice_enabled(10)
    incoming = event("Помоги выбрать подарок", mid=2)
    await bot.handle(incoming)
    bot.provider.answer.assert_awaited_once()
    incoming.reply.assert_awaited_once()


async def test_natural_stop_resume_and_reset(bot):
    bot.history.add(10, 100, "Мой секрет", "Ответ")
    await bot.handle(event("Забудь нашу переписку", mid=1))
    assert not bot.history.messages(10)
    await bot.handle(event("Не отвечай мне", mid=2))
    await bot.handle(event("Обычное сообщение", mid=3))
    bot.provider.answer.assert_not_awaited()
    await bot.handle(event("Давай продолжим", mid=4))
    assert not bot.history.is_paused(10)


async def test_reply_sends_actual_telegram_entities(bot):
    incoming = event()
    await bot.reply(incoming, "**Важное** и *подробности*")
    sent = incoming.reply.await_args
    assert "**" not in sent.args[0]
    assert any(isinstance(e, MessageEntityBold) for e in sent.kwargs["formatting_entities"])


async def test_entity_error_fallback_keeps_spoiler_private(bot):
    incoming = event()
    incoming.reply.side_effect = [errors.EntityBoundsInvalidError(None), None]
    await bot.reply(incoming, "**Важное**: ||скрытый ответ||")
    assert incoming.reply.await_count == 2
    fallback = incoming.reply.await_args
    assert "скрытый ответ" not in fallback.args[0]
    assert "Важное" in fallback.args[0]
    assert "formatting_entities" not in fallback.kwargs


async def test_non_formatting_telegram_errors_are_not_retried(bot):
    incoming = event()
    incoming.reply.side_effect = errors.ChatWriteForbiddenError(None)
    with pytest.raises(errors.ChatWriteForbiddenError):
        await bot.reply(incoming, "**Ответ**")
    assert incoming.reply.await_count == 1
