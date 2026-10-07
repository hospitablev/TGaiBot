import asyncio
import json
import time
from array import array
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telethon.tl.types import MessageActionPhoneCall

from tgaibot.archive_web import create_app
from tgaibot.call_audio import REACTIONS, CallRecorder, ReactionBank
from tgaibot.call_brain import CallBrain
from tgaibot.calls import CallBridge
from tgaibot.metrics import Metrics
from tgaibot.provider import Provider
from tgaibot.storage import History
from tgaibot.userbot import Userbot


def make_bridge(settings, history):
    bot = Userbot(settings, SimpleNamespace(), SimpleNamespace(answer=AsyncMock()), history, 99)
    return CallBridge(bot)


def test_missed_call_service_survives_reset_and_is_scoped(history):
    message = SimpleNamespace(
        action=MessageActionPhoneCall(call_id=42), out=False, date=datetime.now(timezone.utc)
    )
    history.calls.service(10, message)
    history.calls.service(10, message)
    assert len(history.calls.list(10)) == 1
    assert "точная причина неизвестна" in history.calls.context(10)
    assert not history.calls.context(11)
    history.reset(10)
    assert history.calls.list(10)[0]["status"] == "missed"


def test_ring_callbacks_merge_in_both_orders(history):
    one = history.calls.incoming(10)
    history.calls.update(one, "connecting")
    assert history.calls.incoming(10, 123) == one
    two = history.calls.incoming(11, 124)
    assert history.calls.incoming(11) == two
    assert len(history.calls.list(10)) == 1


def test_restart_marks_incomplete_call_and_preserves_transcript(settings, history):
    identifier = history.calls.incoming(10, 123)
    history.calls.update(identifier, "active")
    history.calls.transcript(identifier, "user", "Привет")
    reopened = History(settings)
    row = reopened.calls.list(10)[0]
    assert row["reason"] == "restart" and row["status"] == "failed"
    assert row["transcript"][0]["text"] == "Привет"
    reopened.close()


async def test_barge_in_cancels_playback_once_and_queues_new_speech(settings, history):
    bridge = make_bridge(settings, history)
    bridge.identifier = history.calls.incoming(10)
    bridge.listening = True
    bridge.turn_task = asyncio.create_task(asyncio.sleep(60))
    frame = array("h", [1000] * 960).tobytes()
    for _ in range(25):
        bridge.feed(frame)
    for _ in range(30):
        bridge.feed(b"\0" * 1920)
    assert bridge.turn_task.cancelling() == 1
    assert bridge.queue.qsize() == 1
    assert history.calls.list(10)[0]["interruptions"] == 1
    await asyncio.gather(bridge.turn_task, return_exceptions=True)
    await bridge.close()


async def test_incoming_allowlist_and_busy_reasons(settings, history):
    bridge = make_bridge(settings, history)
    bridge.enabled = True
    bridge.allowed = {10}
    bridge.chat_id = 11
    await bridge.incoming(10)
    await bridge.incoming(12)
    assert history.calls.list(10)[0]["reason"] == "busy"
    assert history.calls.list(12)[0]["reason"] == "not_allowed"
    await bridge.close()


async def test_recording_encodes_both_tracks_and_recovers_partial(settings, monkeypatch):
    from tgaibot import call_audio

    outputs = []

    def encode(args):
        from pathlib import Path

        assert "amix=inputs=2:duration=longest:normalize=0" in args
        target = Path(args[-1])
        target.write_bytes(b"OggS-test")
        outputs.append(target)

    monkeypatch.setattr(call_audio, "run_tool", encode)
    recorder = CallRecorder(settings, 1)
    recorder.write("user", array("h", [500] * 960).tobytes())
    recorder.write("assistant", array("h", [200] * 960).tobytes())
    path = await recorder.finish()
    assert path.read_bytes().startswith(b"OggS") and len(outputs) == 1
    assert not (recorder.root / "user.pcm").exists()
    assert await recorder.finish() is None


def test_reaction_cache_changes_with_speed_and_has_dozen_categories(settings):
    first = ReactionBank(settings, None)
    second = ReactionBank(replace(settings, fish_speed=1), None)
    assert first.root != second.root and len(REACTIONS) >= 30
    assert first.path("../../outside") is None


async def test_call_api_auth_scope_and_audio_path_checks(settings, history, tmp_path):
    settings = replace(settings, archive_password="test-private-password-123")
    identifier = history.calls.incoming(10)
    history.calls.transcript(identifier, "user", "Проверка")
    outside = tmp_path / "outside.ogg"
    outside.write_bytes(b"OggS")
    history.calls.recording(identifier, outside, 1)
    app = create_app(settings, history=history)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        assert (await client.get("/api/dialogs/10/calls")).status_code == 401
        assert (await client.get(f"/api/calls/{identifier}/audio")).status_code == 401
        await client.post(
            "/api/login",
            json={"password": settings.archive_password},
            headers={"x-archive-request": "1"},
        )
        assert (await client.get("/api/dialogs/11/calls")).json() == []
        data = (await client.get("/api/dialogs/10/calls")).json()[0]
        assert data["transcript"][0]["text"] == "Проверка" and "recording" not in data
        assert (await client.get(f"/api/calls/{identifier}/audio")).status_code == 404


def test_gemini_fractional_token_prices_and_separate_model_totals(tmp_path):
    metrics = Metrics(tmp_path)
    metrics.record(
        "call",
        model="gemini-3.8-flash",
        usage={
            "prompt_tokens": 1000000,
            "completion_tokens": 1000000,
            "prompt_tokens_details": {"cached_tokens": 100000, "cache_write_tokens": 200000},
        },
    )
    row = metrics.db.execute("SELECT cost_nano FROM events").fetchone()
    assert row[0] == 91_830_000
    assert "gemini-3.8-flash" in metrics.report(all_time=True)
    assert "$0.0161" in metrics.report()
    metrics.close()


async def test_only_known_flash_backend_alias_is_allowed(settings):
    provider = Provider(replace(settings, model="gemini-3.8-flash"))
    assert provider.matches_model("gemini-3.8-flash-n")
    assert not provider.matches_model("gemini-other")
    await provider.close()


async def test_call_task_delegation_only_after_valid_plan(settings, history):
    bridge = make_bridge(settings, history)
    from unittest.mock import Mock

    tasks = SimpleNamespace(create=Mock(return_value={"task_id": 8}))
    bridge.bot.agent = SimpleNamespace(tasks=tasks)
    plan = {
        "transcript": "Подготовь PDF о поездке в Алматы",
        "speech": "",
        "action": "task",
        "query": "Подготовь PDF о поездке в Алматы",
        "reaction": "none",
    }
    result = await bridge.brain.act(10, -123, plan, lambda: True)
    assert "8" in result and "сохранил" in result
    assert tasks.create.call_args.args[:2] == (10, -123)
    with pytest.raises(asyncio.CancelledError):
        await bridge.brain.act(10, -124, plan, lambda: False)
    assert tasks.create.call_count == 1
    await bridge.close()


async def test_fast_path_failure_uses_local_transcript_and_sonnet(
    settings, history, tmp_path, monkeypatch
):
    from tgaibot import call_brain

    bridge = make_bridge(settings, history)
    bridge.brain.retry_after = time.monotonic() + 60
    text = "Как дела?"
    monkeypatch.setattr(
        call_brain,
        "prepare_isolated",
        AsyncMock(
            return_value=SimpleNamespace(text="Расшифровка речи (может содержать ошибки):\n" + text)
        ),
    )
    bridge.bot.provider.answer.return_value = json.dumps(
        {
            "transcript": "wrong",
            "speech": "Я на связи.",
            "action": "reply",
            "query": "",
            "reaction": "none",
        }
    )
    path = tmp_path / "input.wav"
    path.write_bytes(b"test")
    plan = await bridge.brain.understand(10, path)
    assert plan["transcript"] == text and plan["speech"] == "Я на связи."
    await bridge.close()


def test_call_plan_rejects_fabricated_tool_and_missing_query():
    with pytest.raises(ValueError):
        CallBrain.parse('{"action":"shell"}')


async def test_recording_failed_conversion_keeps_original_tracks(settings, monkeypatch):
    from pathlib import Path

    from tgaibot import call_audio

    def fail(args):
        Path(args[-1]).write_bytes(b"partial")
        raise RuntimeError("encoder interrupted")

    monkeypatch.setattr(call_audio, "run_tool", fail)
    recorder = CallRecorder(settings, 2)
    recorder.write("user", array("h", [500] * 960).tobytes())
    with pytest.raises(RuntimeError):
        await recorder.finish()
    assert (recorder.root / "user.pcm").stat().st_size > 0
    assert not (recorder.root / "recording.ogg").exists()


async def test_video_perception_preserves_frames_on_failure(settings):
    from unittest.mock import Mock

    from tgaibot.call_brain import Perception

    perception = Perception(settings)
    prepared = SimpleNamespace(text="Текст речи", images=["frame"], content=Mock(return_value=[]))
    perception.provider.answer = AsyncMock(side_effect=RuntimeError("unavailable"))
    await perception.video(prepared, "Что здесь?")
    assert prepared.images == ["frame"] and prepared.text == "Текст речи"
    perception.retry_after = 0
    perception.provider.answer = AsyncMock(return_value="В кадре кот")
    await perception.video(prepared, "Что здесь?")
    assert prepared.images == [] and "В кадре кот" in prepared.text
    await perception.close()
