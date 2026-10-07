import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgaibot.memory import Memory
from tgaibot.storage import History


def source(history, text, mid=1, user=1, created=None):
    created = created or time.time()
    history.archive_incoming(
        user,
        SimpleNamespace(id=mid, message=text, date=datetime.fromtimestamp(created, timezone.utc)),
    )
    history.knowledge.enqueue(user, mid, text, created=created)
    return {"message_id": mid, "text": text, "created": created, "version": 1}


def fact(source, slot="profile.first_name", value="Анна", operation="assert"):
    return {
        "source_id": source["message_id"],
        "slot": slot,
        "value": value,
        "quote": source["text"],
        "operation": operation,
        "tags": ["имя"],
    }


def apply(history, s, facts, user=1):
    return history.knowledge.apply(user, [s], facts, epoch=history.epoch(user), history=history)


def test_versions_conflicts_and_grounded_correction(history):
    first = source(history, "Меня зовут Анна")
    apply(history, first, [fact(first)])
    second = source(history, "Меня зовут Мария", 2)
    apply(history, second, [fact(second, value="Мария", operation="correct")])
    assert {f["status"] for f in history.knowledge.facts(1)} == {"conflict"}
    third = source(history, "Исправление: меня зовут Мария", 3)
    apply(history, third, [fact(third, value="Мария", operation="correct")])
    current = history.knowledge.facts(1)
    assert [(f["value"], f["status"]) for f in current] == [("Мария", "active")]
    assert history.db.execute("SELECT count(*) FROM memory_facts").fetchone()[0] == 3
    assert not apply(history, third, [fact(third, value="Мария")])  # replay is idempotent


def test_invalid_evidence_cannot_partially_update_card(history):
    s = source(history, "Меня зовут Анна. Мне 22 года.")
    bad = fact(s, slot="profile.age", value="22")
    bad["quote"] = "Мне 35 лет"
    with pytest.raises(ValueError, match="quote"):
        apply(history, s, [fact(s), bad])
    assert history.knowledge.facts(1) == []
    assert history.knowledge.pending()[1]


def test_source_ids_are_scoped_not_model_controlled(history):
    own = source(history, "Меня зовут Анна", user=1)
    other = source(history, "Меня зовут Мария", mid=2, user=2)
    with pytest.raises(ValueError, match="source"):
        apply(history, own, [fact(other, value="Мария")])
    assert history.knowledge.facts(1) == []


def test_delete_latest_correction_does_not_restore_old_fact(history):
    first = source(history, "Меня зовут Анна")
    apply(history, first, [fact(first)])
    corrected = source(history, "Исправление: меня зовут Мария", 2)
    apply(history, corrected, [fact(corrected, value="Мария", operation="correct")])
    history.revise([2], 1)
    assert history.knowledge.facts(1) == []
    assert history.db.execute("SELECT status FROM memory_facts ORDER BY id").fetchall() == [
        ("superseded",),
        ("revoked",),
    ]


def test_edit_during_extraction_rejects_old_result_and_requeues(history):
    s = source(history, "Меня зовут Анна")
    epoch = history.epoch(1)
    history.revise([1], 1, replacement="Меня зовут Мария")
    assert not history.knowledge.apply(1, [s], [fact(s)], epoch=epoch, history=history)
    user, batch = history.knowledge.pending()
    assert user == 1 and batch[0]["text"] == "Меня зовут Мария" and batch[0]["version"] == 2


def test_age_is_timestamped_birth_date_calculated_and_plans_become_stale(history):
    stamp = datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp()
    s = source(history, "Мне 22 года, я родилась 2003-10-08, планирую поездку", created=stamp)
    apply(
        history,
        s,
        [
            fact(s, "profile.age", "22"),
            fact(s, "profile.birth_date", "2003-10-08"),
            fact(s, "plan.trip", "Поездка"),
        ],
    )
    before = {f["slot"]: f for f in history.knowledge.facts(1, now=stamp)}
    assert before["profile.birth_date"]["calculated_age"] == 22
    assert before["profile.age"]["observed_on"].startswith("2026-10-07")
    after = {f["slot"]: f for f in history.knowledge.facts(1, now=stamp + 32 * 86400)}
    assert after["profile.birth_date"]["calculated_age"] == 23
    assert after["profile.age"]["value"] == "22"  # never blindly increment
    assert after["plan.trip"]["stale"]


def test_forget_blocks_old_context_and_can_be_explicitly_relearned(history):
    s = source(history, "Меня зовут Анна")
    apply(history, s, [fact(s)])
    history.add(1, 1, s["text"], "Привет, Анна")
    history.add(1, 2, "Как меня зовут?", "Анна")  # assistant's later echo must not resurrect it
    other = source(history, "Меня зовут Мария", user=2, mid=3)
    apply(history, other, [fact(other, value="Мария")], user=2)
    identifier = history.knowledge.facts(1)[0]["id"]
    with pytest.raises(ValueError):
        history.knowledge.forget(2, identifier, history)
    history.knowledge.forget(1, identifier, history)
    assert history.messages(1) == []
    assert history.retrieve(1, "Анна") == []
    assert history.knowledge.facts(1) == []
    assert history.knowledge.facts(2)[0]["value"] == "Мария"
    reminder = source(history, "Меня зовут Анна", mid=4)
    apply(history, reminder, [fact(reminder)])
    assert history.knowledge.facts(1) == []
    explicit = source(history, "Запомни: меня зовут Анна", mid=5)
    apply(history, explicit, [fact(explicit)])
    assert history.knowledge.facts(1)[0]["value"] == "Анна"
    assert history.retrieve(1, "Анна") == []  # old raw context stays excluded


def test_reset_and_restart_preserve_scope(settings, history):
    s = source(history, "Меня зовут Анна")
    apply(history, s, [fact(s)])
    source(history, "Мой город Алматы", mid=2)
    reopened = History(settings)
    assert reopened.knowledge.facts(1)[0]["value"] == "Анна"
    assert reopened.knowledge.pending()[1][0]["message_id"] == 2
    reopened.close()
    history.reset(1)
    assert history.knowledge.facts(1) == [] and history.knowledge.pending() == (None, [])


async def test_worker_schema_retry_and_context(settings, history):
    s = source(history, "Меня зовут Анна")
    provider = SimpleNamespace(answer=AsyncMock(return_value=json.dumps({"facts": [fact(s)]})))
    memory = Memory(settings, history, provider)
    assert await memory.process_pending()
    assert not await memory.process_pending()
    context, _ = await memory.context(1, "Как меня зовут?")
    assert "Анна" in context[0]["content"]
    assert provider.answer.await_args.kwargs["usage_kind"] == "memory"
    bad = source(history, "Моя фамилия Иванова", mid=2)
    provider.answer.return_value = "bad json"
    assert await memory.process_pending()
    assert not await memory.process_pending()  # persistent five-minute backoff
    assert history.knowledge.facts(1)[0]["value"] == "Анна"
    assert (
        history.db.execute(
            "SELECT attempts FROM memory_sources WHERE message_id=?", (bad["message_id"],)
        ).fetchone()[0]
        == 1
    )


async def test_forget_during_model_extraction_cannot_resurrect(settings, history):
    s = source(history, "Меня зовут Анна")

    async def answer(*args, **kwargs):
        history.reset(1)
        return json.dumps({"facts": [fact(s)]})

    memory = Memory(settings, history, SimpleNamespace(answer=answer))
    await memory.process_pending()
    assert history.knowledge.facts(1) == []


def test_forwarded_text_is_not_enqueued(settings, history):
    memory = Memory(settings, history, SimpleNamespace())
    memory.enqueue(
        1,
        SimpleNamespace(
            id=1, message="Меня зовут Анна", date=datetime.now(timezone.utc), fwd_from=object()
        ),
    )
    assert history.knowledge.pending() == (None, [])


async def test_memory_search_never_crosses_people(settings, history):
    first = source(history, "Меня зовут Анна", user=1)
    apply(history, first, [fact(first)])
    second = source(history, "Меня зовут Мария", mid=2, user=2)
    apply(history, second, [fact(second, value="Мария")], user=2)
    history.add(2, 3, "Секретный проект второго человека", "Ответ")
    result = Memory(settings, history, SimpleNamespace()).search(1, "имя секретный проект")
    assert "Мария" not in json.dumps(result, ensure_ascii=False)
    assert result["archive"] == []


def test_voice_confirmation_is_scoped_and_revoked_with_confirmation(history):
    s = source(history, "Меня зовут Анна")
    s["origin"] = "voice"
    apply(history, s, [fact(s)])
    stored = history.knowledge.facts(1)[0]
    assert stored["status"] == "unconfirmed"
    with pytest.raises(ValueError):
        history.knowledge.confirm(2, stored["id"], 90)
    source(history, "Подтверди факт 1", mid=90)
    history.knowledge.confirm(1, stored["id"], 90)
    assert history.knowledge.facts(1)[0]["status"] == "active"
    history.revise([90], 1)
    assert history.knowledge.facts(1) == []
    assert "profile.first_name" in history.knowledge.withdrawn(1)


def test_voice_enqueue_does_not_trust_forwarded_audio(settings, history):
    memory = Memory(settings, history, SimpleNamespace())
    message = SimpleNamespace(id=1, message="", date=datetime.now(timezone.utc), voice=True)
    prepared = SimpleNamespace(text="Расшифровка речи (может содержать ошибки):\nМеня зовут Анна")
    memory.enqueue_voice(1, message, prepared)
    assert history.knowledge.pending()[1][0]["origin"] == "voice"
    message.id, message.fwd_from = 2, object()
    memory.enqueue_voice(1, message, prepared)
    assert len(history.knowledge.pending()[1]) == 1


def test_birth_date_requires_full_evidence_and_conflicting_age_is_not_resolved(history):
    s = source(history, "Родилась в 2003 году")
    apply(history, s, [fact(s, "profile.birth_date", "2003-01-01")])
    assert history.knowledge.facts(1) == []
    s = source(history, "Я родилась 8 октября 2003 года, мне 40 лет", mid=2)
    apply(history, s, [fact(s, "profile.birth_date", "2003-10-08"), fact(s, "profile.age", "40")])
    values = history.knowledge.facts(1)
    assert {v["status"] for v in values} == {"conflict"}
    assert all("calculated_age" not in v for v in values)


async def test_forgotten_age_cannot_be_rederived_from_birth_date(settings, history):
    s = source(history, "Я родилась 2003-10-08")
    apply(history, s, [fact(s, "profile.birth_date", "2003-10-08")])
    history.knowledge.forget_slot(1, "profile.age", history)
    memory = Memory(settings, history, SimpleNamespace())
    assert memory.search(1, "возраст")["facts"] == []
    context, _ = await memory.context(1, "Сколько мне лет?")
    assert "2003-10-08" not in json.dumps(context)
    assert "calculated_age" not in history.knowledge.facts(1)[0]


def test_delayed_extraction_does_not_overwrite_newer_fact(history):
    old = source(history, "Меня зовут Анна", created=time.time() - 100)
    new = source(history, "Исправление: меня зовут Мария", mid=2)
    apply(history, new, [fact(new, value="Мария", operation="correct")])
    apply(history, old, [fact(old)])
    assert [(f["value"], f["status"]) for f in history.knowledge.facts(1)] == [("Мария", "active")]


async def test_web_profile_is_protected_and_scoped(settings, history):
    from dataclasses import replace

    import httpx

    from tgaibot.archive_web import create_app

    s = source(history, "Меня зовут Анна")
    apply(history, s, [fact(s)])
    settings = replace(settings, archive_password="private-test-password-123")
    app = create_app(settings, history=history)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/dialogs/1/memory")).status_code == 401
        await client.post(
            "/api/login",
            json={"password": settings.archive_password},
            headers={"x-archive-request": "1"},
        )
        assert (await client.get("/api/dialogs/1/memory")).json()["facts"][0]["value"] == "Анна"
        assert (await client.get("/api/dialogs/2/memory")).json()["facts"] == []
