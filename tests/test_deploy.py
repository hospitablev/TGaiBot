from dataclasses import replace

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from tgaibot.deploy import export_session
from tgaibot.userbot import make_client


def test_session_export_keeps_secret_out_of_console(settings, capsys):
    settings.data_dir.mkdir()
    session = SQLiteSession(str(settings.data_dir / "telegram"))
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    session.save()
    expected = StringSession.save(session)
    session.close()
    export_session(settings)
    assert expected not in capsys.readouterr().out
    output = (settings.data_dir / "railway-session.env").read_text()
    assert output == "TELEGRAM_SESSION=" + expected + "\n"
    client = make_client(
        replace(
            settings, telegram_api_id=123, telegram_api_hash="a" * 32, telegram_session=expected
        )
    )
    assert isinstance(client.session, StringSession)


def test_missing_session_export_has_actionable_error(settings):
    with pytest.raises(RuntimeError, match="login"):
        export_session(settings)
