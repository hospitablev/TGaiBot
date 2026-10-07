from array import array

import pytest
from telethon import functions, types

from tgaibot.calls import BYTES_PER_SECOND, SpeechBuffer, call_readiness
from tgaibot.userbot import IncomingOnlyTelegramClient


def test_voice_segmentation_and_silence():
    buffer = SpeechBuffer()
    assert buffer.feed(b"\0" * 96000) is None
    assert buffer.feed(array("h", [1000] * 24000).tobytes()) is None
    segment = buffer.feed(b"\0" * BYTES_PER_SECOND)
    assert segment and len(segment) == int(1.62 * BYTES_PER_SECOND)
    assert not buffer.data


def test_no_unbounded_recording():
    buffer = SpeechBuffer()
    voiced = array("h", [1000] * 48000).tobytes()
    for _ in range(19):
        assert buffer.feed(voiced) is None
    assert len(buffer.feed(voiced)) == 20 * BYTES_PER_SECOND


async def test_outbound_call_rpc_blocked_before_network():
    client = IncomingOnlyTelegramClient(None, 1, "0" * 32)
    request = functions.phone.RequestCallRequest(
        user_id=types.InputUser(123, 456),
        random_id=1,
        g_a_hash=b"x" * 32,
        protocol=types.PhoneCallProtocol(
            min_layer=1, max_layer=1, library_versions=[], udp_p2p=True, udp_reflector=True
        ),
    )
    with pytest.raises(RuntimeError, match="Исходящие звонки запрещены"):
        await client(request)


async def test_real_pytgcalls_accepts_guarded_telethon_client_without_login():
    sdk = pytest.importorskip("pytgcalls")
    client = IncomingOnlyTelegramClient(None, 1, "0" * 32)
    calls = sdk.PyTgCalls(client)
    assert calls.mtproto_client is client
    assert calls._app.package_name == "telethon"
    assert calls._app._bind_client.__class__.__name__ == "TelethonClient"
    calls.executor.shutdown(wait=False)
    await client.disconnect()


def test_calls_require_separate_enablement_and_allowlist(settings):
    missing = call_readiness(settings)
    assert "FISH_API_KEY" in missing
    assert "CALL_ALLOWED_USER_IDS" in missing
    assert "ENABLE_TRANSCRIPTION=true" in missing


async def test_call_pipeline_with_mock_transport(settings, history, monkeypatch):
    pytest.importorskip("pytgcalls")
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tgaibot import calls
    from tgaibot.userbot import Userbot

    bot = Userbot(
        settings,
        SimpleNamespace(),
        SimpleNamespace(answer=AsyncMock(return_value="Короткий ответ")),
        history,
        99,
    )
    bridge = calls.CallBridge(bot)
    bridge.chat_id = 10
    bridge.app = SimpleNamespace(play=AsyncMock(), record=AsyncMock(), leave_call=AsyncMock())

    async def speak(chat, text, directory):
        if "Короткий ответ" in text:
            asyncio.get_running_loop().call_soon(bridge.ended.set)
        else:
            bridge.queue.put_nowait(array("h", [1000] * 24000).tobytes())

    bridge.speak = AsyncMock(side_effect=speak)
    bridge.brain.understand = AsyncMock(
        return_value={
            "transcript": "Тестовая реплика",
            "speech": "Короткий ответ",
            "action": "reply",
            "query": "",
            "reaction": "none",
        }
    )
    await asyncio.wait_for(bridge.session(10), timeout=5)
    bridge.app.play.assert_awaited_once()
    bridge.app.record.assert_awaited_once()
    bridge.app.leave_call.assert_awaited_once()
    assert bridge.speak.await_count == 2
    assert history.messages(10)[-1]["content"] == "Короткий ответ"
    assert bridge.chat_id is None
    await bridge.close()
