import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgaibot.agent import Agent
from tgaibot.conversation import background_request, task_control
from tgaibot.tasks import TaskManager, TaskStore


def response(name, args, identifier="1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def finish(summary="Файл готов", status="completed"):
    return response("finish_task", {"status": status, "summary": summary}, "end")


@pytest.fixture
async def manager(settings, history):
    provider = SimpleNamespace(step=AsyncMock())
    client = SimpleNamespace(send_file=AsyncMock(), send_message=AsyncMock())
    search = SimpleNamespace(web=AsyncMock(return_value={"results": []}), places=AsyncMock())
    agent = Agent(settings, provider, search, client)
    manager = TaskManager(settings, agent, history)
    agent.tasks = manager
    yield manager
    await manager.close()


def create(manager, user=10, source=1, delay=0):
    return manager.create(
        user,
        source,
        [],
        title="Подготовить документ",
        instruction="Создай текстовый файл с приветствием",
        delay_minutes=delay,
    )["task_id"]


def reopen(manager):
    manager.store.close()
    manager.store = TaskStore(manager.settings)


async def test_changed_chat_stops_saved_tasks_without_affecting_other_user(manager):
    task = create(manager)
    other = create(manager, user=20)
    await manager.invalidate_context(10)
    assert manager.store.get(10, task)["status"] == "cancelled"
    assert manager.store.get(20, other)["status"] == "queued"
    manager.agent.provider.step.assert_not_awaited()


async def test_background_plan_action_completion_and_notification(manager):
    task_id = create(manager)
    manager.agent.provider.step.side_effect = [
        response("task_plan", {"steps": ["Подготовить текст", "Отправить файл"]}),
        response("create_text_file", {"name": "hello", "content": "Привет!", "format": "txt"}),
        finish(),
    ]
    await manager.run_task(10, task_id)
    detail = manager.store.describe(10, task_id)
    assert detail["status"] == "Готово" and len(detail["plan"]) == 2
    assert detail["actions"][1]["result"]["sent"].startswith("файл")
    row = manager.store.get(10, task_id)
    await manager.notify(row)
    assert manager.store.get(10, task_id)["notification"] == "sent"
    manager.agent.client.send_file.assert_awaited_once()
    manager.agent.client.send_message.assert_awaited_once()
    assert manager.agent.client.send_message.await_args.args[0] == 10
    assert "Файл готов" in manager.report(10, task_id)
    assert manager.history.messages(10)


async def test_task_creation_is_deduplicated_and_scoped(manager):
    task_id = create(manager)
    assert create(manager) == task_id
    assert manager.store.list(20) == []
    with pytest.raises(ValueError):
        manager.store.describe(20, task_id)
    with pytest.raises(ValueError):
        await manager.cancel(20, task_id)
    assert manager.store.get(10, task_id)["status"] == "queued"


async def test_creation_limits_and_delay_survive_restart(manager):
    task_id = create(manager, delay=60)
    create(manager, source=2)
    create(manager, source=3)
    with pytest.raises(ValueError):
        create(manager, source=4)
    reopen(manager)
    assert manager.store.get(10, task_id)["due"] > time.time() + 3500
    assert manager.store.get(10, task_id)["status"] == "queued"


async def test_restart_marks_uncertain_external_action_for_attention(manager):
    task_id = create(manager)
    manager.store.update(task_id, status="running")
    manager.store.start_action(task_id, "key", "create_pdf")
    reopen(manager)
    assert manager.store.get(10, task_id)["status"] == "attention"
    assert "Проверь" in manager.report(10, task_id)
    manager.agent.client.send_file.assert_not_awaited()


async def test_restart_recovers_read_only_work(manager):
    task_id = create(manager)
    manager.store.update(task_id, status="running")
    manager.store.start_action(task_id, "key", "search_web")
    reopen(manager)
    assert manager.store.get(10, task_id)["status"] == "queued"


async def test_completed_tool_not_resent_after_checkpoint_crash(manager):
    task_id = create(manager)
    manager.agent.provider.step.side_effect = [
        response("create_text_file", {"name": "hello", "content": "Привет!", "format": "txt"}),
        finish(),
    ]
    original = manager.store.save_state
    crashed = False

    def crash_after_send(tid, state):
        nonlocal crashed
        if not crashed and state["conversation"][-1]["role"] == "tool":
            crashed = True
            raise asyncio.CancelledError
        original(tid, state)

    manager.store.save_state = crash_after_send
    with pytest.raises(asyncio.CancelledError):
        await manager.run_task(10, task_id)
    reopen(manager)
    await manager.run_task(10, task_id)
    manager.agent.client.send_file.assert_awaited_once()
    assert manager.store.get(10, task_id)["status"] == "completed"


async def test_uncertain_delivery_is_not_retried(manager):
    task_id = create(manager)
    manager.agent.client.send_file.side_effect = ConnectionError()
    manager.agent.provider.step.return_value = response(
        "create_text_file", {"name": "hello", "content": "Привет!", "format": "txt"}
    )
    await manager.run_task(10, task_id)
    assert manager.store.get(10, task_id)["status"] == "attention"
    manager.agent.client.send_file.assert_awaited_once()
    with pytest.raises(ValueError):
        manager.resume(10, task_id, "продолжай")


async def test_clarification_resumes_same_task_with_new_instruction(manager):
    task_id = create(manager)
    manager.agent.provider.step.side_effect = [
        finish("Какой город?", "needs_input"),
        finish("Готово для Алматы"),
    ]
    await manager.run_task(10, task_id)
    manager.resume(10, task_id, "Алматы")
    await manager.run_task(10, task_id)
    assert manager.store.get(10, task_id)["status"] == "completed"
    assert any(m.get("content") == "Алматы" for m in manager.agent.provider.step.await_args.args[0])


async def test_cancel_running_task_and_query_status_during_work(manager):
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    manager.agent.provider.step.side_effect = slow
    task_id = create(manager)
    manager.start()
    await asyncio.wait_for(started.wait(), 2)
    assert "Выполняется" in manager.report(10)
    await manager.cancel(10, task_id)
    assert manager.store.get(10, task_id)["status"] == "cancelled"
    manager.agent.client.send_file.assert_not_awaited()


async def test_pause_and_reset_cancel_work_and_erase_only_owners_tasks(manager):
    task_id = create(manager)
    other = create(manager, user=20)
    manager.history.pause(10)
    await manager.suspend(10, forget=True)
    assert manager.store.list(10) == []
    assert manager.store.get(20, other)
    with pytest.raises(ValueError):
        manager.store.get(10, task_id)


async def test_no_notification_replay_after_restart(manager):
    task_id = create(manager)
    manager.store.update(task_id, status="completed", result="Готово", notification="sending")
    reopen(manager)
    row = manager.store.get(10, task_id)
    assert row["notification"] == "uncertain"
    await manager.notify(row)
    manager.agent.client.send_message.assert_not_awaited()


async def test_model_step_budget_persists(manager):
    task_id = create(manager)
    manager.store.update(task_id, steps=16)
    await manager.run_task(10, task_id)
    manager.agent.provider.step.assert_not_awaited()
    assert manager.store.get(10, task_id)["status"] == "needs_input"
    with pytest.raises(ValueError):
        manager.resume(10, task_id, "ещё")


async def test_places_are_restored_from_completed_action_journal(manager):
    task_id = create(manager)
    args = {"query": "Алматы"}
    response_value = response("search_places", args)
    state = json.loads(manager.store.get(10, task_id)["state"])
    state["conversation"].append(response_value)
    state["pending"] = response_value["tool_calls"]
    manager.store.save_state(task_id, state)
    key = hashlib.sha256(
        json.dumps(["search_places", args], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    manager.store.start_action(task_id, key, "search_places")
    manager.store.finish_action(
        task_id,
        key,
        {
            "places": [
                {
                    "place_id": "place-1",
                    "name": "Алматы",
                    "latitude": 43.2,
                    "longitude": 76.9,
                    "url": "https://www.openstreetmap.org",
                }
            ]
        },
    )
    manager.agent.provider.step.side_effect = [
        response("send_location", {"place_id": "place-1"}),
        finish(),
    ]
    await manager.run_task(10, task_id)
    manager.agent.search.places.assert_not_awaited()
    assert manager.agent.client.send_file.await_args.args[1].geo_point.lat == 43.2


async def test_foreground_model_can_create_and_inspect_background_task(manager):
    manager.agent.provider.step.side_effect = [
        response(
            "create_task",
            {"title": "Документ", "instruction": "Подготовь документ", "delay_minutes": 0},
        ),
        {"role": "assistant", "content": "Принял задачу"},
    ]
    assert (
        await manager.agent.answer([{"role": "user", "content": "Займись в фоне"}], 10, 1)
        == "Принял задачу"
    )
    assert len(manager.store.list(10)) == 1


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Что сделано?", ("list", None)),
        ("Статус задачи 3", ("get", 3)),
        ("Отмени задачу #7", ("cancel", 7)),
        ("расскажи про задачи по математике", None),
    ],
)
def test_plain_language_controls(text, expected):
    assert task_control(text) == expected


def test_background_intent_is_explicit():
    assert background_request("В фоне сравни варианты поездки") == "сравни варианты поездки"
    assert background_request("Сделай в фоне: PDF с планом") == "PDF с планом"
    assert background_request("объясни что такое фоновые задачи") is None


async def test_telegram_background_ack_status_and_cancel_need_no_model(manager):
    from tgaibot.userbot import Userbot

    bot = Userbot(
        manager.settings,
        manager.agent.client,
        manager.agent.provider,
        manager.history,
        99,
        agent=manager.agent,
    )

    def event(mid, text):
        msg = SimpleNamespace(id=mid, message=text, date=datetime.now(timezone.utc), media=None)
        return SimpleNamespace(id=mid, sender_id=10, raw_text=text, message=msg, reply=AsyncMock())

    incoming = event(1, "В фоне подготовь план выходного")
    await bot.process(incoming, [incoming.message])
    assert len(manager.store.list(10)) == 1
    assert "Принял задачу #" in incoming.reply.await_args.args[0]
    status = event(2, "Что сделано?")
    await bot.process(status, [status.message])
    assert "В очереди" in status.reply.await_args.args[0]
    cancel = event(3, "Отмени задачу 1")
    await bot.process(cancel, [cancel.message])
    assert manager.store.get(10, 1)["status"] == "cancelled"
    manager.agent.provider.step.assert_not_awaited()


async def test_reset_does_not_start_next_queued_task_for_same_user(manager):
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    manager.agent.provider.step.side_effect = slow
    create(manager)
    create(manager, source=2)
    manager.start()
    await asyncio.wait_for(started.wait(), 2)
    await manager.suspend(10, forget=True)
    await asyncio.sleep(0)
    assert manager.store.list(10) == []
    assert manager.agent.provider.step.await_count == 1


async def test_notifications_pause_with_account(manager):
    task_id = create(manager)
    manager.store.update(task_id, status="completed", result="Готово", notification="pending")
    manager.history.pause(10)
    await manager.notify(manager.store.get(10, task_id))
    manager.agent.client.send_message.assert_not_awaited()
    assert manager.store.get(10, task_id)["notification"] == "pending"


async def test_resume_cannot_overfill_queue(manager):
    task_id = create(manager)
    manager.store.update(task_id, status="needs_input")
    for source in [2, 3, 4]:
        create(manager, source=source)
    with pytest.raises(ValueError, match="Очередь"):
        manager.resume(10, task_id, "Алматы")


async def test_pause_preserves_remaining_calls_in_batch(manager):
    task_id = create(manager)
    batch = response("search_web", {"query": "first"})
    batch["tool_calls"] += response("search_web", {"query": "second"}, "2")["tool_calls"]
    manager.agent.provider.step.side_effect = [batch, finish()]

    async def pause_on_first(query):
        if query == "first":
            manager.history.pause(10)
        return {"results": []}

    manager.agent.search.web.side_effect = pause_on_first
    await manager.run_task(10, task_id)
    row = manager.store.get(10, task_id)
    state = json.loads(row["state"])
    assert row["status"] == "queued"
    assert state["index"] == 1 and len(state["pending"]) == 2
    manager.history.pause(10, False)
    await manager.run_task(10, task_id)
    assert manager.store.get(10, task_id)["status"] == "completed"
    assert manager.agent.search.web.await_count == 2


async def test_worker_waits_until_scheduled_time(manager):
    future = create(manager, delay=60)
    ready = create(manager, source=2)
    finished = asyncio.Event()

    async def execute(user_id, task_id):
        assert task_id == ready
        manager.store.update(task_id, status="completed")
        finished.set()

    manager.run_task = AsyncMock(side_effect=execute)
    manager.start()
    await asyncio.wait_for(finished.wait(), 2)
    assert manager.store.get(10, future)["status"] == "queued"
    assert manager.run_task.await_count == 1
