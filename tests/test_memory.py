import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tgaibot.memory import FIELDS, Memory


def summary(**fields):
    value = {key: [] for key in FIELDS}
    value.update(fields)
    return value


def seed(history, user=1, count=36):
    for number in range(count):
        history.add(user, number, f"Мой проект Орион, задача {number}", f"Обсудили шаг {number}")


async def test_summary_updates_and_fresh_context_stays_verbatim(settings, history):
    seed(history)
    remembered = summary(
        user_claims=["user turn 1: проект Орион"], preferences=["user turn 2: кратко"]
    )
    provider = SimpleNamespace(answer=AsyncMock(return_value=json.dumps(remembered)))
    memory = Memory(settings, history, provider)
    context, notice = await memory.context(1, "Обсудим Орион")
    assert provider.answer.await_count == 1
    assert history.summary(1)[0] == 12
    assert history.summary(1)[1] == remembered
    assert context[-1]["content"] == "Обсудили шаг 35"
    assert context[-2]["content"] == "Мой проект Орион, задача 35"
    assert len([m for m in context if m["role"] == "assistant"]) == 24
    assert history.db.execute("SELECT count(*) FROM turns").fetchone()[0] == 36
    assert notice == ""


async def test_memory_isolation_and_explicit_corrections(settings, history):
    seed(history)
    seed(history, user=2)
    previous = summary(user_claims=["user turn 1: бюджет 100"], decisions=["turn 1: прежний план"])
    history.save_summary(1, 1, previous, 0)
    for number in range(36, 45):
        history.add(1, number, "Исправление: бюджет 200, прежнее решение отменено", "Принято")
    updated = summary(
        user_claims=["user turn 21: бюджет 200"], corrections=["user: 100 заменено на 200"]
    )
    provider = SimpleNamespace(answer=AsyncMock(return_value=json.dumps(updated)))
    context, _ = await Memory(settings, history, provider).context(1, "бюджет")
    sent = provider.answer.await_args.args[0][0]["content"]
    assert "previous_memory" in sent and "бюджет 100" in sent
    assert "Выводы ассистента" in sent and "исправления" in sent
    assert history.summary(1)[1] == updated
    assert history.summary(2) == (0, {})
    assert "бюджет 200" in json.dumps(context, ensure_ascii=False)


async def test_invalid_summary_does_not_erase_previous(settings, history):
    seed(history)
    previous = summary(user_claims=["Важное условие"])
    history.save_summary(1, 1, previous, 0)
    provider = SimpleNamespace(answer=AsyncMock(return_value="not valid json"))
    context, notice = await Memory(settings, history, provider).context(1, "условие")
    assert history.summary(1) == (1, previous)
    assert "не удалось обновить" in notice
    assert context[-1]["content"] == "Обсудили шаг 35"


def test_retrieval_scoped_and_reset_removes_all_memory(settings, history, tmp_path):
    history.add(1, 1, "Договор Альфа: бюджет 73000", "Ответ")
    history.add(2, 1, "Договор Альфа: секрет второго пользователя", "Другой ответ")
    history.save_summary(1, 1, summary(preferences=["Пиши коротко"]), 0)
    history.set_voice(1, True)
    original = tmp_path / "original.txt"
    original.write_text("local original")
    history.archive_attachment(1, 1, original)
    history.archive_attachment(2, 1, original)
    found = history.retrieve(1, "Альфа")
    assert len(found) == 1 and "73000" in found[0][1]
    history.reset(1)
    assert history.retrieve(1, "Альфа") == []
    assert history.summary(1) == (0, {})
    assert not history.voice_enabled(1)
    assert not (settings.data_dir / "attachments" / "1").exists()
    assert (settings.data_dir / "attachments" / "2").exists()
    assert history.retrieve(2, "Альфа")
    assert not history.save_summary(1, 1, summary(user_claims=["stale"]), 0)


async def test_no_aggressive_summary_for_short_chat(settings, history):
    seed(history, count=10)
    provider = SimpleNamespace(answer=AsyncMock())
    context, _ = await Memory(settings, history, provider).context(1, "продолжай")
    provider.answer.assert_not_awaited()
    assert len(context) == 20


async def test_oversized_summary_rejected(settings, history):
    seed(history)
    provider = SimpleNamespace(
        answer=AsyncMock(return_value=json.dumps(summary(user_claims=["x" * 20000])))
    )
    await Memory(settings, history, provider).context(1, "продолжай")
    assert history.summary(1) == (0, {})


async def test_bad_summary_backoff_does_not_charge_every_message(settings, history):
    seed(history)
    provider = SimpleNamespace(answer=AsyncMock(return_value="invalid"))
    memory = Memory(settings, history, provider)
    await memory.context(1, "продолжай")
    context, notice = await memory.context(1, "а дальше?")
    assert provider.answer.await_count == 1
    assert notice == ""
    assert context[-1]["content"] == "Обсудили шаг 35"


def test_edit_and_album_delete_invalidate_summary_but_keep_other_chat(history):
    from datetime import datetime, timezone

    for mid in (1, 2):
        history.archive_incoming(
            1, SimpleNamespace(id=mid, date=datetime.now(timezone.utc), message="старый секрет")
        )
    history.add(1, 1, "старый секрет", "ответ", source_ids=[1, 2])
    history.add(2, 1, "другой диалог", "ответ")
    history.save_summary(1, 1, summary(user_claims=["старый секрет"]), 0)
    assert history.revise([2], 1) == {1}
    assert history.summary(1) == (0, {})
    assert history.epoch(1) == 1
    assert "старый секрет" not in json.dumps(history.messages(1), ensure_ascii=False)
    assert history.retrieve(1, "секрет") == []
    assert history.messages(2)[0]["content"] == "другой диалог"
