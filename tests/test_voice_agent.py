import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tgaibot.agent import Agent
from tgaibot.tts import TTSError, display_speech, speech_text


def action(name, args, identifier="1"):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def response(*calls):
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


@pytest.fixture
def voice_agent(settings, history, tmp_path):
    settings = replace(settings, fish_key="fake-fish-key")
    agent = Agent(
        settings,
        SimpleNamespace(step=AsyncMock()),
        SimpleNamespace(),
        SimpleNamespace(send_file=AsyncMock()),
    )
    agent.memory = SimpleNamespace(history=history)
    agent.tts = SimpleNamespace(voice_note=AsyncMock(return_value=(tmp_path / "answer.ogg", False)))
    return agent


async def test_combined_request_saves_mode_sends_once_and_keeps_clean_transcript(
    voice_agent, history
):
    script = "[warm tone] Ночь укрыла сад. [long pause] Звёзды тихо спят."
    speak = action("send_voice", {"text": script}, "2")
    voice_agent.provider.step.side_effect = [
        response(action("set_reply_mode", {"mode": "voice"}), speak),
        response(speak, action("send_voice", {"text": "Лишняя озвучка"}, "3")),
        {"content": "Отправлено"},
    ]
    answer = await voice_agent.answer(
        [{"role": "user", "content": "Можешь отвечать голосом и рассказать стих короткий"}], 10, 9
    )
    assert history.voice_enabled(10) and not history.voice_enabled(11)
    voice_agent.tts.voice_note.assert_awaited_once()
    assert voice_agent.tts.voice_note.await_args.args[0] == script
    voice_agent.client.send_file.assert_awaited_once()
    assert voice_agent.client.send_file.await_args.args[0] == 10
    assert voice_agent.client.send_file.await_args.kwargs["voice_note"] is True
    assert voice_agent.client.send_file.await_args.kwargs["reply_to"] == 9
    assert answer.voice_attempted and "Ночь укрыла сад" in answer
    assert "[long pause]" not in answer and "[warm tone]" not in answer


async def test_one_off_does_not_change_mode_and_new_answer_uses_saved_preference(
    voice_agent, history
):
    voice_agent.provider.step.side_effect = [
        response(action("send_voice", {"text": "[chuckle] Привет"})),
        {"content": "Готово"},
    ]
    await voice_agent.answer([], 10, 1)
    assert not history.voice_enabled(10)
    history.set_voice(10, True)
    voice_agent.provider.step.side_effect = [{"content": "Ответ"}]
    await voice_agent.answer([], 10, 2)
    assert (
        "Текущий режим: голос и текст" in voice_agent.provider.step.await_args.args[0][0]["content"]
    )


async def test_switch_to_text_keeps_substantive_answer_without_audio(voice_agent, history):
    history.set_voice(10, True)
    voice_agent.provider.step.side_effect = [
        response(action("set_reply_mode", {"mode": "text"})),
        {"content": "Два плюс два равно четырём"},
    ]
    result = await voice_agent.answer([], 10, 1)
    assert not history.voice_enabled(10)
    assert "четырём" in result and not result.voice_attempted
    voice_agent.tts.voice_note.assert_not_awaited()


async def test_tts_failure_retains_script_even_if_followup_model_fails(voice_agent):
    voice_agent.tts.voice_note.side_effect = TTSError("failure")
    voice_agent.provider.step.side_effect = [
        response(action("send_voice", {"text": "[chuckle] Текст истории"})),
        RuntimeError("provider down"),
    ]
    result = await voice_agent.answer([], 10, 1)
    assert "Текст истории" in result and "[chuckle]" not in result
    assert result.voice_attempted
    voice_agent.client.send_file.assert_not_awaited()


async def test_deleted_during_synthesis_cannot_send(voice_agent):
    current = True

    async def tts(*args):
        nonlocal current
        current = False
        return ("unused.ogg", False)

    voice_agent.tts.voice_note.side_effect = tts
    voice_agent.provider.step.side_effect = [
        response(action("send_voice", {"text": "Привет"})),
        {"content": "Готово"},
    ]
    await voice_agent.answer([], 10, 1, lambda: current)
    voice_agent.client.send_file.assert_not_awaited()


async def test_uncertain_voice_delivery_is_not_retried(voice_agent):
    speak = action("send_voice", {"text": "Привет"})
    voice_agent.provider.step.side_effect = [
        response(speak),
        response(speak),
        {"content": "Не могу подтвердить доставку"},
    ]
    voice_agent.client.send_file.side_effect = ConnectionError()
    result = await voice_agent.answer([], 10, 1)
    assert result.voice_attempted
    voice_agent.client.send_file.assert_awaited_once()
    voice_agent.tts.voice_note.assert_awaited_once()


async def test_unavailable_voice_cannot_claim_saved_mode(voice_agent, history):
    voice_agent.settings = replace(voice_agent.settings, voice_replies="off")
    voice_agent.provider.step.side_effect = [
        response(action("set_reply_mode", {"mode": "voice"})),
        {"content": "Озвучка недоступна"},
    ]
    await voice_agent.answer([], 10, 1)
    assert not history.voice_enabled(10)
    voice_agent.tts.voice_note.assert_not_awaited()


def test_expressive_tags_survive_tts_but_not_display():
    script = "**Привет!** [chuckle] Вот история. [long pause] Конец."
    spoken, _ = speech_text(script)
    assert "[chuckle]" in spoken and "[long pause]" in spoken and "**" not in spoken
    assert "[chuckle]" not in display_speech(script)
    assert "[chuckle](https://example.com)" == display_speech("[chuckle](https://example.com)")
