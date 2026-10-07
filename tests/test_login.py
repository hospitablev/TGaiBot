from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telethon import errors

from tgaibot import userbot


async def test_interactive_login_2fa_never_echoes_secret(settings, monkeypatch, capsys):
    client = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=False),
        send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="opaque")),
        sign_in=AsyncMock(side_effect=[errors.SessionPasswordNeededError(None), None]),
        get_me=AsyncMock(return_value=SimpleNamespace(bot=False)),
    )
    secrets = ["+12345678900", "54321", "test-2fa-secret"]
    monkeypatch.setattr(userbot, "make_client", lambda settings: client)
    monkeypatch.setattr(userbot.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(userbot, "getpass", Mock(side_effect=secrets))
    await userbot.login(settings)
    assert client.sign_in.await_count == 2
    assert client.sign_in.await_args.kwargs == {"password": "test-2fa-secret"}
    assert all(secret not in capsys.readouterr().out for secret in secrets)
    client.disconnect.assert_awaited_once()
