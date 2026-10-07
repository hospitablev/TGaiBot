import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types

from tgaibot.turns import CURRENT_TURN, DeliveryUncertain, Turn
from tgaibot.userbot import IncomingOnlyTelegramClient, Userbot


def incoming(text="Привет", mid=1, reply_to=None):
    message = SimpleNamespace(
        id=mid,
        message=text,
        media=None,
        reply_to=reply_to,
        via_bot_id=None,
        action=None,
        date=datetime.now(timezone.utc),
    )
    return SimpleNamespace(
        is_private=True,
        out=False,
        sender_id=10,
        id=mid,
        message=message,
        raw_text=text,
        reply=AsyncMock(),
        get_sender=AsyncMock(return_value=SimpleNamespace(bot=False)),
    )


@pytest.fixture
async def bot(settings, history):
    bot = Userbot(
        settings,
        SimpleNamespace(send_read_acknowledge=AsyncMock()),
        SimpleNamespace(answer=AsyncMock(return_value="Ответ")),
        history,
        99,
    )
    bot.queue.QUIET = 0.03
    bot.queue.MAX_WAIT = 0.1
    yield bot
    await bot.queue.close()


async def test_word_burst_is_one_turn_replies_to_last_and_reads_all(bot):
    messages = [incoming(t, i) for i, t in enumerate(("даров", "чё как", "шо делаем"), 1)]
    await asyncio.gather(*(bot.handle(m) for m in messages))
    bot.provider.answer.assert_awaited_once()
    prompt = bot.provider.answer.call_args.args[0][-1]["content"]
    assert all(t in str(prompt) for t in ("даров", "чё как", "шо делаем"))
    messages[0].reply.assert_not_awaited()
    messages[-1].reply.assert_awaited_once()
    bot.client.send_read_acknowledge.assert_awaited_once_with(10, max_id=3)
    assert bot.history.db.execute("SELECT count(*) FROM usage").fetchone()[0] == 1
    assert bot.history.db.execute("SELECT count(*) FROM turn_sources").fetchone()[0] == 3


async def test_new_phrase_restarts_only_unsent_model_work(bot):
    started = asyncio.Event()

    async def answer(messages):
        if bot.provider.answer.await_count == 1:
            started.set()
            await asyncio.Event().wait()
        return "Общий ответ"

    bot.provider.answer.side_effect = answer
    first, second = incoming("даров"), incoming("шо делаем", 2)
    one = asyncio.create_task(bot.handle(first))
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.wait_for(asyncio.gather(one, bot.handle(second)), 2)
    first.reply.assert_not_awaited()
    second.reply.assert_awaited_once()
    assert "даров" in str(bot.provider.answer.call_args.args[0])
    assert "шо делаем" in str(bot.provider.answer.call_args.args[0])
    assert bot.history.db.execute("SELECT count(*) FROM usage").fetchone()[0] == 1


async def test_message_during_send_gets_its_own_quoted_reply(bot):
    sending, release = asyncio.Event(), asyncio.Event()
    first, second = incoming("Привет"), incoming("Второй вопрос", 2)

    async def slow_reply(*a, **kw):
        sending.set()
        await release.wait()

    first.reply.side_effect = slow_reply
    one = asyncio.create_task(bot.handle(first))
    await asyncio.wait_for(sending.wait(), 1)
    two = asyncio.create_task(bot.handle(second))
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.wait_for(asyncio.gather(one, two), 2)
    assert bot.provider.answer.await_count == 2
    first.reply.assert_awaited_once()
    second.reply.assert_awaited_once()


async def test_explicit_telegram_replies_are_not_combined(bot):
    first, second = incoming("Первый"), incoming("Про другое", 2, reply_to=object())
    await asyncio.gather(bot.handle(first), bot.handle(second))
    assert bot.provider.answer.await_count == 2
    first.reply.assert_awaited_once()
    second.reply.assert_awaited_once()


async def test_restart_resumes_cached_answer_without_another_model_request(bot, settings):
    first = incoming()
    failed = asyncio.Event()

    async def interrupted(*a, **kw):
        failed.set()
        raise OSError("connection lost")

    first.reply.side_effect = interrupted
    pending = asyncio.create_task(bot.handle(first))
    await asyncio.wait_for(failed.wait(), 1)
    await bot.queue.close()
    await asyncio.gather(pending, return_exceptions=True)
    assert not bot.history.seen(10, 1)
    fresh = Userbot(
        settings, SimpleNamespace(), SimpleNamespace(answer=AsyncMock()), bot.history, 99
    )
    new_event = incoming()
    fresh.queue.live[(10, 1)] = (new_event, new_event.message)
    bot.history.db.execute("UPDATE reply_jobs SET due=0")
    bot.history.db.commit()
    row = bot.history.db.execute("SELECT id,user_id,ids,epoch,data FROM reply_jobs").fetchone()
    try:
        await fresh.queue.run(Turn(fresh.queue, row))
        fresh.provider.answer.assert_not_awaited()
        new_event.reply.assert_awaited_once()
        assert new_event.reply.call_args.args[0] == "Ответ"
        assert bot.history.seen(10, 1)
        assert len(bot.history.messages(10)) == 2
    finally:
        await fresh.queue.close()


def active_turn(bot):
    event = incoming()
    job = bot.queue.enqueue(event, [event.message])
    row = bot.history.db.execute(
        "SELECT id,user_id,ids,epoch,data FROM reply_jobs WHERE id=?", (job,)
    ).fetchone()
    return Turn(bot.queue, row)


def request(random_id=123):
    return functions.messages.SendMessageRequest(
        peer=types.InputPeerUser(10, 20), message="Ответ", random_id=random_id
    )


def receipt():
    return types.UpdateShortSentMessage(
        id=50, pts=1, pts_count=1, date=datetime.now(timezone.utc), out=True
    )


async def test_outbox_retries_exact_request_and_reuses_confirmed_result(bot):
    turn = active_turn(bot)
    send = AsyncMock(side_effect=OSError("lost response"))
    with pytest.raises(OSError):
        await turn.rpc(request(123), send)
    turn.sending("final")
    send.side_effect = None
    send.return_value = receipt()
    await turn.rpc(request(456), send)
    assert send.call_args.args[0].random_id == 123
    turn.sending("final")
    result = await turn.rpc(request(789), send)
    assert result.id == 50 and send.await_count == 2


async def test_outbox_unknown_receipt_never_changes_random_id(bot):
    turn = active_turn(bot)
    send = AsyncMock(side_effect=errors.RandomIdDuplicateError(request(123)))
    with pytest.raises(DeliveryUncertain):
        await turn.rpc(request(), send)
    assert bot.history.db.execute("SELECT response FROM reply_outbox").fetchone()[0] is None


async def test_outbox_rejects_changed_payload_instead_of_misattributing_receipt(bot):
    turn = active_turn(bot)
    send = AsyncMock(return_value=receipt())
    await turn.rpc(request(), send)
    turn.sending("final")
    changed = request(999)
    changed.message = "Другой ответ"
    with pytest.raises(DeliveryUncertain):
        await turn.rpc(changed, send)
    send.assert_awaited_once()


async def test_recovery_fetches_real_telegram_message_by_persisted_id(bot):
    client = IncomingOnlyTelegramClient(None, 1, "0" * 32)
    user = types.User(id=10, first_name="Test", access_hash=20)
    client._mb_entity_cache.set_self_user(99, False, 100)
    message = types.Message(
        id=1,
        peer_id=types.PeerUser(10),
        from_id=types.PeerUser(10),
        message="Привет",
        date=datetime.now(timezone.utc),
    )
    message._finish_init(client, {10: user}, types.InputPeerUser(10, 20))
    client.get_messages = AsyncMock(return_value=[message])
    client.get_entity = AsyncMock(return_value=user)
    client.get_input_entity = AsyncMock(return_value=types.InputPeerUser(10, 20))
    turn = active_turn(bot)
    bot.queue.live.clear()
    bot.client = client
    try:
        event, messages = await bot.queue.materialize(turn)
        assert event.id == 1 and event.sender_id == 10 and messages[0].message == "Привет"
        client.get_messages.assert_awaited_once_with(10, ids=[1])
    finally:
        await client.disconnect()


async def test_offline_catchup_queues_only_unhandled_recent_incoming(bot):
    client = IncomingOnlyTelegramClient(None, 1, "0" * 32)
    client._mb_entity_cache.set_self_user(99, False, 100)
    user = types.User(id=10, first_name="Test", access_hash=20)
    now = datetime.now(timezone.utc)
    messages = []
    for mid, out, seconds in [(4, True, 1), (3, False, 2), (2, False, 3), (1, False, 600)]:
        msg = types.Message(
            id=mid,
            peer_id=types.PeerUser(10),
            from_id=types.PeerUser(10),
            message="Hi",
            out=out,
            date=now - timedelta(seconds=seconds),
        )
        msg._finish_init(client, {10: user}, types.InputPeerUser(10, 20))
        messages.append(msg)

    async def dialogs():
        yield SimpleNamespace(is_user=True, date=now, id=10)

    async def fetch(*a, **kw):
        for msg in messages:
            yield msg

    client.iter_dialogs = dialogs
    client.iter_messages = fetch
    bot.client = client
    bot.history.mark_seen(10, 2)
    bot.history.db.execute("INSERT INTO reply_meta VALUES('online',?)", (time.time() - 120,))
    bot.history.db.commit()
    try:
        await bot.queue.restore()
        assert bot.queue.caught_up
        assert bot.history.db.execute("SELECT message_id FROM reply_sources").fetchall() == [(3,)]
    finally:
        await client.disconnect()


async def test_guarded_client_routes_real_send_rpc_through_outbox(bot, monkeypatch):
    from telethon import TelegramClient

    send = AsyncMock(return_value=receipt())
    monkeypatch.setattr(TelegramClient, "__call__", send)
    client = IncomingOnlyTelegramClient(None, 1, "0" * 32)
    turn = active_turn(bot)
    token = CURRENT_TURN.set(turn)
    try:
        await client(request())
        turn.sending("final")
        await client(request(999))
        assert send.await_count == 1
    finally:
        CURRENT_TURN.reset(token)
        await client.disconnect()


async def test_tool_receipt_and_model_step_survive_restart(bot):
    turn = active_turn(bot)
    produce = AsyncMock(return_value={"content": "Ответ"})
    execute = AsyncMock(return_value={"sent": "файл"})
    await turn.step(0, produce)
    await turn.tool("create_text_file", {"text": "hi"}, {"1": "map"}, execute)
    row = bot.history.db.execute(
        "SELECT id,user_id,ids,epoch,data FROM reply_jobs WHERE id=?", (turn.id,)
    ).fetchone()
    restored = Turn(bot.queue, row)
    places = {}
    assert await restored.step(0, produce) == {"content": "Ответ"}
    assert await restored.tool("create_text_file", {"text": "hi"}, places, execute) == {
        "sent": "файл"
    }
    assert places == {"1": "map"} and produce.await_count == execute.await_count == 1


async def test_budget_exhaustion_defers_instead_of_dropping(bot):
    for _ in range(6):
        assert bot.history.reserve(10, min_interval=0)
    job = bot.queue.enqueue(incoming(), [incoming().message])
    row = bot.history.db.execute(
        "SELECT id,user_id,ids,epoch,data FROM reply_jobs WHERE id=?", (job,)
    ).fetchone()
    await bot.queue.run(Turn(bot.queue, row))
    status, due = bot.history.db.execute("SELECT status,due FROM reply_jobs").fetchone()
    assert status == "pending" and due > time.time() + 500
    assert not bot.history.seen(10, 1)
    bot.provider.answer.assert_not_awaited()


async def test_deleted_pending_message_cancels_without_read_receipt(bot):
    bot.history.revise([1], 10)
    event = incoming()
    await bot.handle(event)
    event.reply.assert_not_awaited()
    bot.client.send_read_acknowledge.assert_not_awaited()


async def test_voice_is_marked_listened_only_after_successful_processing(
    bot, monkeypatch, tmp_path
):
    from tgaibot import userbot
    from tgaibot.media import Prepared

    event = incoming("")
    event.message.media = object()
    event.message.voice = True
    bot.client = AsyncMock()
    from contextlib import nullcontext
    from unittest.mock import Mock

    bot.client.action = Mock(return_value=nullcontext())
    bot.client.is_connected = Mock(return_value=True)
    bot.download = AsyncMock(return_value=tmp_path / "sound.ogg")
    (tmp_path / "sound.ogg").write_bytes(b"audio")
    prepared = Prepared(text="Расшифровка речи (может содержать ошибки):\nПривет")

    async def prepare(*a):
        bot.client.assert_not_awaited()
        return prepared

    monkeypatch.setattr(userbot, "prepare_isolated", prepare)
    await bot.handle(event)
    assert isinstance(bot.client.call_args.args[0], functions.messages.ReadMessageContentsRequest)
    assert bot.client.call_args.args[0].id == [1]


async def test_failed_audio_processing_does_not_mark_listened(bot, monkeypatch, tmp_path):
    from contextlib import nullcontext
    from unittest.mock import Mock

    from tgaibot import userbot
    from tgaibot.media import MediaError

    event = incoming("")
    event.message.media, event.message.voice = object(), True
    bot.client = AsyncMock()
    bot.client.action = Mock(return_value=nullcontext())
    bot.client.is_connected = Mock(return_value=True)
    path = tmp_path / "audio.ogg"
    path.write_bytes(b"audio")
    bot.download = AsyncMock(return_value=path)
    monkeypatch.setattr(
        userbot, "prepare_isolated", AsyncMock(side_effect=MediaError("Не удалось разобрать звук"))
    )
    await bot.handle(event)
    bot.client.assert_not_awaited()
    bot.client.send_read_acknowledge.assert_awaited_once()
    event.reply.assert_awaited_once()


async def test_finished_voice_file_is_reused_after_restart(bot, settings, tmp_path, monkeypatch):
    from pathlib import Path

    from tgaibot import tts
    from tgaibot.tts import FishTTS

    turn = active_turn(bot)
    turn.sending("auto_voice")
    fish = FishTTS(settings)

    async def synthesize(text, path):
        path.write_bytes(b"ID3-test")
        return path

    fish.synthesize = AsyncMock(side_effect=synthesize)

    def encode(args):
        Path(args[-1]).write_bytes(b"OggS-test")

    monkeypatch.setattr(tts, "run_tool", encode)
    token = CURRENT_TURN.set(turn)
    try:
        first, _ = await fish.voice_note("Привет", tmp_path)
        second, _ = await fish.voice_note("Привет", tmp_path)
        assert first == second and first.is_file()
        fish.synthesize.assert_awaited_once()
    finally:
        CURRENT_TURN.reset(token)
        await fish.close()
